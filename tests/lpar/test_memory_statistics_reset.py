"""Bounded statistics observations and one irreversible native reset attempt."""

import asyncio
import math
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError
from hmcpctl.ssh.transport import HMCCLIError

ROW = "min_io_entitled_mem=1108,optimal_io_entitled_mem=1108,max_io_entitled_mem_usage=152\n"


@pytest.mark.parametrize(
    "values", [{}, {"wait_minutes": 0}, {"detail_level": 1}, {"detail_level": 4}]
)
def test_request_exact_contract(values):
    from hmcpctl.documents.lpar_memory import LparMemoryStatisticsReset

    request = LparMemoryStatisticsReset(**values)
    assert request.model_dump(exclude_none=True) == values
    with pytest.raises(ValidationError):
        request.wait_minutes = 3


@pytest.mark.parametrize(
    "values",
    [
        {"wait_minutes": True},
        {"wait_minutes": -1},
        {"wait_minutes": 1.5},
        {"wait_minutes": "0"},
        {"detail_level": False},
        {"detail_level": 0},
        {"detail_level": 5},
        {"detail_level": 2.0},
        {"force": True},
    ],
)
def test_request_rejects_unknown_or_untyped(values):
    from hmcpctl.documents.lpar_memory import LparMemoryStatisticsReset

    with pytest.raises(ValidationError):
        LparMemoryStatisticsReset(**values)


@pytest.mark.parametrize(
    "raw,usable",
    [
        (ROW, True),
        (ROW.rstrip("\n") + "\r\n", True),
        ("min_io_entitled_mem=0", True),
        ("min_io_entitled_mem=" + "9" * 4096, True),
        ('"extra=a,b=c",min_io_entitled_mem=1', True),
        ("min_io_entitled_mem= 1", False),
        ("min_io_entitled_mem=١", False),
        ("min_io_entitled_mem=-1", False),
        ("min_io_entitled_mem=1.0", False),
        ("min_io_entitled_mem=1e2", False),
        ("min_io_entitled_mem=N/A", False),
        ("min_io_entitled_mem=", False),
        ("extra=1", False),
    ],
)
def test_statistics_rows(raw, usable):
    from hmcpctl.operations.lpar.memory_statistics import parse_statistics

    result = parse_statistics(raw)
    assert result["raw"] == raw
    assert result["usable"] is usable
    assert result["error"] is None
    assert result["entries"][0]["key"] == (
        "extra" if raw.startswith(('"extra', "extra=")) else "min_io_entitled_mem"
    )
    expected = (
        "a,b=c"
        if raw.startswith('"')
        else raw.split("=", 1)[1].split(",", 1)[0].rstrip("\r\n")
    )
    assert result["entries"][0]["value"] == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "\n",
        ROW + "\n",
        ROW + ROW,
        "min_io_entitled_mem=1,",
        "min_io_entitled_mem=1,min_io_entitled_mem=2",
        "min_io_entitled_mem=1,bare",
        'min_io_entitled_mem=1,extra=a"b',
        '"min_io_entitled_mem=1',
        '"min_io_entitled_mem=1"extra',
        " min_io_entitled_mem=1",
        "min_io_entitled_mem=1\x00",
        "min_io_entitled_mem=1\t",
        "min_io_entitled_mem=1\ud800",
        "min_io_entitled_mem=1,extra=" + "x" * 4097,
        "min_io_entitled_mem=1," + ",".join(f"k{i}=v" for i in range(256)),
        "min_io_entitled_mem=1," + "k" * 129 + "=v",
        "min_io_entitled_mem=1," + ",".join(f"k{i}=" + "x" * 4096 for i in range(16)),
    ],
)
def test_statistics_invalid_rows_remain_unavailable(raw):
    from hmcpctl.operations.lpar.memory_statistics import parse_statistics

    result = parse_statistics(raw)
    assert not result["usable"]
    assert result["entries"] == [] and result["missing"] is None
    assert result["error"]


def test_statistics_preserve_order_values_and_missing():
    from hmcpctl.operations.lpar.memory_statistics import parse_statistics

    result = parse_statistics(
        "extra=  text  ,min_io_entitled_mem=0,Optimal_io_entitled_mem=22"
    )
    assert result["entries"] == [
        {"key": "extra", "value": "  text  ", "kind": "opaque"},
        {"key": "min_io_entitled_mem", "value": "0", "kind": "numeric"},
        {"key": "Optimal_io_entitled_mem", "value": "22", "kind": "opaque"},
    ]
    assert result["missing"] == ["optimal_io_entitled_mem", "max_io_entitled_mem_usage"]


@pytest.mark.asyncio
async def test_native_commands_are_fixed(monkeypatch):
    from hmcpctl.documents.lpar_memory import LparMemoryStatisticsReset
    from hmcpctl.ssh import memory

    command = AsyncMock(return_value=ROW)
    monkeypatch.setattr(memory, "run_hmc_command", command)
    config = HMCConfig.from_mapping({})
    await memory.read_lpar_memory_statistics(config, "sys ' quoted", 7)
    assert (
        command.await_args.args[1]
        == "lshwres -r mem -m 'sys '\"'\"' quoted' --level lpar --filter lpar_ids=7 --stat"
    )
    await memory.reset_lpar_memory_statistics_native(
        config, "sys-R1", 7, LparMemoryStatisticsReset(wait_minutes=0, detail_level=1)
    )
    assert (
        command.await_args.args[1] == "chhwres -r mem -m sys-R1 -o rs --id 7 -w 0 -d 1"
    )
    assert command.await_args.kwargs == {"max_output_bytes": 65536}
    await memory.reset_lpar_memory_statistics_native(
        config, "sys-R1", 7, LparMemoryStatisticsReset()
    )
    assert command.await_args.args[1] == "chhwres -r mem -m sys-R1 -o rs --id 7"


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [math.inf, -math.inf, math.nan, 0, -1])
async def test_native_finite_ssh_timeout_admission(monkeypatch, timeout):
    from hmcpctl.documents.lpar_memory import LparMemoryStatisticsReset
    from hmcpctl.ssh import memory

    transport = AsyncMock()
    monkeypatch.setattr(memory, "run_hmc_command", transport)
    config = HMCConfig.from_mapping({"ssh_timeout": timeout})
    for call in (
        memory.read_lpar_memory_statistics(config, "sys-R1", 7),
        memory.reset_lpar_memory_statistics_native(
            config, "sys-R1", 7, LparMemoryStatisticsReset()
        ),
    ):
        with pytest.raises(ValueError, match="finite positive"):
            await call
    transport.assert_not_awaited()


SYSTEM = "aaaaaaaa-0000-0000-0000-000000000001"
PARTITION = "bbbbbbbb-0000-0000-0000-000000000002"


@pytest.fixture
def reset_boundary(monkeypatch):
    resource = {
        "PartitionName": "guest-R1",
        "PartitionID": "7",
        "PartitionType": "AIX/Linux",
        "PartitionState": "running",
        "PartitionMemoryConfiguration": {"SharedMemoryEnabled": "true"},
    }
    state = SimpleNamespace(
        resource=resource,
        resets=0,
        reads=0,
        statistics=0,
        native_error=None,
        before=ROW,
        after=ROW,
        foreign=False,
        kind="LogicalPartition",
        drift=None,
        after_error=None,
    )
    system = {
        "UUID": SYSTEM,
        "Resource": {
            "SystemName": "sys-R1",
            "AssociatedSystemCapabilities": {"ActiveMemorySharingCapable": "true"},
        },
    }
    state.system = system

    async def read(uuid):
        state.reads += 1
        if state.resets and state.after_error:
            raise state.after_error
        result = deepcopy(state.resource)
        if state.drift and state.reads > 1:
            result[state.drift[0]] = state.drift[1]
        entry = {"UUID": PARTITION, "Resource": result}
        if hasattr(state, "reported_kind"):
            entry["ResourceType"] = state.reported_kind
        return entry

    async def command(config, text, **kwargs):
        if text.startswith("chhwres"):
            state.resets += 1
            if state.native_error:
                raise state.native_error
            return ""
        state.statistics += 1
        return state.after if state.resets else state.before

    state.command = AsyncMock(side_effect=command)
    state.description = AsyncMock(
        side_effect=lambda *args, **kw: (
            "[hmcpctl owner:"
            + ("foreign-owner" if state.foreign else "hmcpctl")
            + " created:2026-10-27]"
        )
    )
    state.hmc = SimpleNamespace(
        config=HMCConfig.from_mapping({}),
        list_system_summaries=AsyncMock(
            return_value=[{"UUID": SYSTEM, "SystemName": "sys-R1"}]
        ),
        get_managed_system=AsyncMock(side_effect=lambda uuid: deepcopy(state.system)),
        list_logical_partitions=AsyncMock(
            side_effect=lambda uuid: (
                [{"UUID": PARTITION, "Resource": deepcopy(resource)}]
                if state.kind == "LogicalPartition"
                else []
            )
        ),
        list_vios=AsyncMock(
            side_effect=lambda uuid: (
                [{"UUID": PARTITION, "Resource": deepcopy(resource)}]
                if state.kind == "VirtualIOServer"
                else []
            )
        ),
        get_logical_partition=AsyncMock(side_effect=read),
        get_vios=AsyncMock(side_effect=read),
    )
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", state.command)
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.ownership.get_lpar_description", state.description
    )
    return state


async def reset_operation(state, *, override=False, **settings):
    from hmcpctl.documents.lpar_memory import LparMemoryStatisticsReset
    from hmcpctl.operations.lpar.memory_statistics import reset_lpar_memory_statistics

    return await reset_lpar_memory_statistics(
        state.hmc,
        "guest-R1",
        LparMemoryStatisticsReset(**settings),
        system_name_or_uuid="sys-R1",
        ownership_override=override,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,platform",
    [
        ("LogicalPartition", "AIX/Linux"),
        ("LogicalPartition", "OS400"),
        ("VirtualIOServer", "Virtual IO Server"),
    ],
)
async def test_single_attempt_and_facts(reset_boundary, kind, platform):
    state = reset_boundary
    state.kind = kind
    state.resource["PartitionType"] = platform
    result = await reset_operation(state)
    assert result["workflow_completed"] is True
    assert result["native_completion"] == "completed"
    assert result["reset_verification"] == "unavailable"
    assert result["identity_preserved"] is True
    assert result["partition_kind"] == kind
    assert state.resets == 1 and state.statistics == 2
    assert state.description.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("PartitionState", "not activated"),
        ("PartitionState", None),
        ("PartitionID", "0"),
        ("PartitionID", True),
        ("PartitionID", "7.0"),
        ("PartitionName", "changed"),
    ],
)
async def test_admission_no_submission(reset_boundary, field, value):
    reset_boundary.resource[field] = value
    with pytest.raises(ValueError):
        await reset_operation(reset_boundary)
    assert reset_boundary.resets == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("timeout", [math.inf, math.nan, 0, -1])
async def test_finite_ssh_timeout_admission(reset_boundary, timeout, override):
    reset_boundary.hmc.config.ssh_timeout = timeout
    with pytest.raises(ValueError, match="finite positive"):
        await reset_operation(reset_boundary, override=override)
    reset_boundary.command.assert_not_awaited()
    reset_boundary.description.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"wait_minutes": 0}, {"detail_level": 1}])
async def test_final_partition_type_options_refused(reset_boundary, options):
    reset_boundary.drift = ("PartitionType", "OS400")
    with pytest.raises(ValueError, match="AIX/Linux"):
        await reset_operation(reset_boundary, **options)
    assert reset_boundary.resets == 0


@pytest.mark.asyncio
async def test_standard_ownership_override(reset_boundary, monkeypatch):
    from hmcpctl.operations.lpar import ownership

    audit = Mock(wraps=ownership.audit.record_ownership_override)
    monkeypatch.setattr(ownership.audit, "record_ownership_override", audit)
    reset_boundary.foreign = True
    with pytest.raises(PermissionError, match="owned by"):
        await reset_operation(reset_boundary)
    assert reset_boundary.resets == 0
    reset_boundary.description.reset_mock()
    assert (await reset_operation(reset_boundary, override=True))["workflow_completed"]
    reset_boundary.description.assert_not_awaited()
    assert audit.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shared,ams", [("false", "true"), (None, "true"), ("true", "false"), ("true", None)]
)
@pytest.mark.parametrize("override", [False, True])
async def test_override_cannot_bypass_mode_or_capability(
    reset_boundary, shared, ams, override
):
    reset_boundary.resource["PartitionMemoryConfiguration"]["SharedMemoryEnabled"] = (
        shared
    )
    reset_boundary.system["Resource"]["AssociatedSystemCapabilities"][
        "ActiveMemorySharingCapable"
    ] = ams
    with pytest.raises(ValueError):
        await reset_operation(reset_boundary, override=override)
    assert reset_boundary.resets == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,completion",
    [
        (HMCCLIError("nonzero", remote_nonzero_completed=True), "nonzero_exit"),
        (HMCCLIError("exit status 1"), "unknown"),
        (TimeoutError(), "unknown"),
        (ConnectionError(), "unknown"),
    ],
)
async def test_uncertain_or_nonzero_submission_never_completes(
    reset_boundary, error, completion
):
    reset_boundary.native_error = error
    result = await reset_operation(reset_boundary)
    assert result["native_completion"] == completion
    assert not result["workflow_completed"]
    assert result["after_statistics"]["usable"]
    assert (reset_boundary.resets, reset_boundary.statistics) == (1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("before", ["extra=1", "min_io_entitled_mem=N/A", "broken"])
async def test_unusable_baseline_never_submits(reset_boundary, before):
    reset_boundary.before = before
    result = await reset_operation(reset_boundary)
    assert result["native_completion"] == "not_submitted"
    assert not result["workflow_completed"]
    assert (reset_boundary.resets, reset_boundary.statistics) == (0, 1)


@pytest.mark.asyncio
async def test_after_read_failure_retains_submission(reset_boundary):
    reset_boundary.after_error = HMCError("raw sensitive body")
    result = await reset_operation(reset_boundary)
    assert result["native_completion"] == "completed"
    assert result["identity_preserved"] is None
    assert not result["workflow_completed"]
    assert "raw sensitive body" not in result["after_error"]
    assert reset_boundary.resets == 1


@pytest.mark.asyncio
async def test_cancellation_is_not_replayed(reset_boundary):
    reset_boundary.native_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await reset_operation(reset_boundary)
    assert reset_boundary.resets == 1


@pytest.mark.asyncio
async def test_finite_transport_silent_peer_no_replay(reset_boundary, monkeypatch):
    import asyncssh

    from hmcpctl.ssh import memory
    from hmcpctl.ssh.transport import run_hmc_command

    class SilentReader:
        async def read(self, size):
            await asyncio.Event().wait()

    process = SimpleNamespace(
        stdout=SilentReader(), stderr=SilentReader(), close=Mock()
    )
    connection = AsyncMock()
    connection.__aenter__.return_value = connection
    connection.__aexit__.return_value = False
    connection.create_process.return_value = process
    connection.abort = Mock()
    monkeypatch.setattr(asyncssh, "connect", Mock(return_value=connection))
    reset_boundary.hmc.config = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "user": "user-R1",
            "password": "test-password",  # pragma: allowlist secret - mocked SSH fixture
            "ssh_timeout": 0.01,
        }
    )

    async def command(config, text, **kwargs):
        if text.startswith("chhwres"):
            reset_boundary.resets += 1
            return await run_hmc_command(config, text, **kwargs)
        return await reset_boundary.command(config, text, **kwargs)

    monkeypatch.setattr(memory, "run_hmc_command", command)
    async with asyncio.timeout(1):
        result = await reset_operation(reset_boundary)
    assert result["native_completion"] == "unknown"
    assert not result["workflow_completed"]
    assert (reset_boundary.resets, reset_boundary.statistics) == (1, 2)
    connection.abort.assert_called_once()
    process.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("PartitionID", {"text": "7", "unexpected": "nested"}),
        ("PartitionName", {"text": "guest-R1", "unexpected": "nested"}),
        ("PartitionState", {"text": "running", "unexpected": "nested"}),
    ],
)
async def test_malformed_scalar_is_not_admitted(reset_boundary, field, value):
    reset_boundary.resource[field] = value
    with pytest.raises(ValueError):
        await reset_operation(reset_boundary)
    assert reset_boundary.resets == 0


@pytest.mark.asyncio
async def test_direct_kind_mismatch_is_not_admitted(reset_boundary):
    reset_boundary.reported_kind = "VirtualIOServer"
    with pytest.raises(ValueError, match="kind"):
        await reset_operation(reset_boundary)
    assert reset_boundary.resets == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_logical_partition", "get_managed_system"])
async def test_missing_detail_is_actionable_before_effect(reset_boundary, method):
    setattr(reset_boundary.hmc, method, AsyncMock(return_value=None))
    with pytest.raises(ValueError, match="unavailable"):
        await reset_operation(reset_boundary)
    assert reset_boundary.resets == 0


@pytest.mark.asyncio
async def test_uuid_system_with_duplicate_cli_name_refuses_before_ssh(reset_boundary):
    from hmcpctl.documents.lpar_memory import LparMemoryStatisticsReset
    from hmcpctl.operations.lpar.memory_statistics import reset_lpar_memory_statistics

    state = reset_boundary
    state.hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM, "SystemName": "sys-R1"},
        {"UUID": "00000000-0000-0000-0000-000000000099", "SystemName": "sys-R1"},
    ]
    with pytest.raises(ValueError, match="unique CLI name"):
        await reset_lpar_memory_statistics(
            state.hmc,
            "guest-R1",
            LparMemoryStatisticsReset(),
            system_name_or_uuid=SYSTEM,
        )
    state.command.assert_not_awaited()
    state.description.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_qualifies_cli_name_from_one_snapshot(reset_boundary):
    from hmcpctl.operations.lpar.memory_statistics import _context

    state = reset_boundary
    context = await _context(state.hmc, SYSTEM.upper(), "guest-R1")
    assert context["system_name"] == "sys-R1"
    state.hmc.list_system_summaries.assert_awaited_once()
    state.command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", [False, True])
async def test_uuid_shaped_cli_name_is_matched_literally(reset_boundary, duplicate):
    from hmcpctl.documents.lpar_memory import LparMemoryStatisticsReset
    from hmcpctl.operations.lpar.memory_statistics import (
        _context,
        reset_lpar_memory_statistics,
    )

    state = reset_boundary
    state.system["Resource"]["SystemName"] = SYSTEM
    summaries = [{"UUID": SYSTEM.upper(), "SystemName": SYSTEM}]
    if duplicate:
        summaries.append(
            {"UUID": "bbbbbbbb-0000-0000-0000-000000000002", "SystemName": SYSTEM}
        )
    state.hmc.list_system_summaries.return_value = summaries
    if duplicate:
        with pytest.raises(ValueError, match="unique CLI name"):
            await reset_lpar_memory_statistics(
                state.hmc,
                "guest-R1",
                LparMemoryStatisticsReset(),
                system_name_or_uuid=SYSTEM,
            )
        state.command.assert_not_awaited()
        state.description.assert_not_awaited()
    else:
        context = await _context(state.hmc, SYSTEM, "guest-R1")
        assert context["system_name"] == SYSTEM
        state.hmc.list_system_summaries.assert_awaited_once()


@pytest.mark.asyncio
async def test_null_uuid_after_submission_retains_known_completion(reset_boundary):
    from hmcpctl.operations.lpar.memory_statistics import parse_statistics

    state = reset_boundary
    valid = {"UUID": SYSTEM, "SystemName": "sys-R1"}
    state.hmc.list_system_summaries.side_effect = lambda: (
        [valid] + ([{"UUID": None, "SystemName": "sys-R1"}] if state.resets else [])
    )
    result = await reset_operation(state)
    assert result["native_completion"] == "completed"
    assert result["submission_error"] is None
    assert not result["workflow_completed"]
    assert result["identity_preserved"] is None
    assert result["before_statistics"] == parse_statistics(ROW)
    assert result["after_statistics"] is None
    assert "ValueError" in result["after_error"]
    assert (state.resets, state.statistics) == (1, 1)
