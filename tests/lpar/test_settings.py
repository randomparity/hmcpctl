"""C01 scalar grammar, scoped transport and post-effect outcomes."""

import shlex
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar import LparSettings
from hmcpctl.operations.lpar.configuration import configure_lpar_settings
from hmcpctl.ssh.lpar import get_lpar_settings, set_lpar_settings
from hmcpctl.ssh.transport import HMCCLIError

NAMES = (
    "allow_perf_collection",
    "lpar_avail_priority",
    "time_ref",
    "simplified_remote_restart_capable",
    "migration_disabled",
)
CONFIG = HMCConfig.from_mapping({})


def test_c01_field_contract_and_explicit_values():
    # Four cited command contexts each own these same five scalar bindings.
    assert tuple(f.name for f in fields(LparSettings)) == NAMES
    assert LparSettings().pairs() == []
    assert LparSettings(allow_perf_collection=False, lpar_avail_priority=0).pairs() == [
        ("allow_perf_collection", 0),
        ("lpar_avail_priority", 0),
    ]
    assert LparSettings(lpar_avail_priority=255).pairs() == [
        ("lpar_avail_priority", 255)
    ]


@pytest.mark.parametrize(
    "name,value",
    [
        (n, v)
        for n in NAMES
        for v in (
            (-1, 256, True, 1.5, "1")
            if n == "lpar_avail_priority"
            else (0, 1, "0", [], 2)
        )
    ],
)
def test_settings_refuse_wrong_types_and_ranges(name, value):
    with pytest.raises(ValueError, match=name):
        LparSettings(**{name: value})


@pytest.mark.asyncio
async def test_settings_transport_quotes_both_record_and_shell(monkeypatch):
    run = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    await set_lpar_settings(
        CONFIG,
        "system;quoted",
        "owned;name",
        LparSettings(time_ref=False, migration_disabled=True),
    )
    words = shlex.split(run.call_args.args[1])
    assert words == [
        "chsyscfg",
        "-r",
        "lpar",
        "-m",
        "system;quoted",
        "-i",
        "name=owned;name,time_ref=0,migration_disabled=1",
    ]
    with pytest.raises(HMCCLIError):
        await set_lpar_settings(CONFIG, "s", "bad,name", LparSettings(time_ref=True))
    assert run.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "0,0,0,0,0",
        "aixlinux,0,256,0,0,0",
        "aixlinux,2,0,0,0,0",
        "aixlinux,0,0,0,0,0\naixlinux,0,0,0,0,0",
        "vioserver,0,0,0,0,0",
        "aixlinux,0,0,0,0,0,extra",
    ],
)
async def test_settings_reader_refuses_unusable_snapshots(monkeypatch, raw):
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", AsyncMock(return_value=raw))
    with pytest.raises(HMCCLIError):
        await get_lpar_settings(CONFIG, "s", "l")


@pytest.mark.asyncio
async def test_settings_reader_typed_roundtrip(monkeypatch):
    run = AsyncMock(return_value="os400,1,255,0,1,0\n")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    result = await get_lpar_settings(CONFIG, "s", "l")
    assert result == LparSettings(True, 255, False, True, False)
    assert shlex.split(run.call_args.args[1])[-1] == "lpar_env," + ",".join(NAMES)


@pytest.fixture
def operation_boundary(monkeypatch):
    resolve = AsyncMock(return_value=("system", "owned"))
    read = AsyncMock(return_value=LparSettings(False, 0, False, False, False))
    write = AsyncMock()
    module = "hmcpctl.operations.lpar.configuration."
    monkeypatch.setattr(module + "resolve_and_authorize_lpar_names", resolve)
    monkeypatch.setattr(module + "get_lpar_settings", read)
    monkeypatch.setattr(module + "set_lpar_settings", write)
    return SimpleNamespace(
        resolve=resolve, read=read, write=write, hmc=SimpleNamespace(config=CONFIG)
    )


@pytest.mark.asyncio
async def test_settings_authorization_and_empty_input_precede_effects(
    operation_boundary,
):
    b = operation_boundary
    with pytest.raises(ValueError):
        await configure_lpar_settings(
            b.hmc, "l", LparSettings(), system_name_or_uuid="s"
        )
    b.resolve.assert_not_awaited()
    b.resolve.side_effect = PermissionError("foreign owner")
    with pytest.raises(PermissionError):
        await configure_lpar_settings(
            b.hmc, "l", LparSettings(time_ref=False), system_name_or_uuid="s"
        )
    b.read.assert_not_awaited()
    b.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,status",
    [
        ("ok", "verified"),
        ("mismatch", "mismatch"),
        ("collateral", "mismatch"),
        ("read", "readback-unavailable"),
        ("write", "write-outcome-unknown"),
    ],
)
async def test_settings_operation_postconditions(operation_boundary, mode, status):
    b = operation_boundary
    baseline = LparSettings(False, 0, False, False, False)
    after = LparSettings(False, 0, True, False, False)
    if mode == "mismatch":
        after = baseline
    if mode == "collateral":
        after = LparSettings(True, 0, True, False, False)
    b.read.side_effect = [
        baseline,
        HMCCLIError("read unavailable") if mode == "read" else after,
    ]
    if mode == "write":
        b.write.side_effect = HMCCLIError("connection lost")
    result = await configure_lpar_settings(
        b.hmc, "l", LparSettings(time_ref=True), system_name_or_uuid="s"
    )
    assert result.status == status
    b.resolve.assert_awaited_once_with(
        b.hmc, "l", system_name_or_uuid="s", ownership_override=False
    )
    b.write.assert_awaited_once()
    assert b.read.await_count == (1 if mode == "write" else 2)
    assert result.requested.time_ref is True


@pytest.mark.asyncio
@pytest.mark.parametrize("keylock", [None, "manual"])
async def test_settings_creation_preserves_requested_resources_and_options(
    monkeypatch, keylock
):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar.configuration import LparSettingsResult
    from hmcpctl.operations.lpar.core import LparCreation, create_and_stamp_lpar

    hmc = SimpleNamespace(
        config=CONFIG,
        find_partition_by_name=AsyncMock(side_effect=[None, {"UUID": "created"}]),
        create_logical_partition=AsyncMock(return_value={"UUID": "created"}),
    )
    module = "hmcpctl.operations.lpar.core."
    monkeypatch.setattr(
        module + "resolve_system_uuid", AsyncMock(return_value="system-uuid")
    )
    monkeypatch.setattr(module + "require_memory_within_system", AsyncMock())
    monkeypatch.setattr(
        module + "resolve_system_cli_name", AsyncMock(return_value="system")
    )
    monkeypatch.setattr(
        module + "stamp_created_lpar_ownership", AsyncMock(return_value=(True, []))
    )
    create = AsyncMock()
    monkeypatch.setattr(module + "create_lpar_via_cli", create)
    settings = LparSettings(allow_perf_collection=False, lpar_avail_priority=0)
    monkeypatch.setattr(module + "get_lpar_settings", AsyncMock(return_value=settings))
    configure = AsyncMock(
        return_value=LparSettingsResult("verified", settings, settings, ())
    )
    monkeypatch.setattr(module + "configure_lpar_settings", configure)
    resources = LparResources(desired_memory=4096)
    result = await create_and_stamp_lpar(
        hmc,
        "system",
        LparCreation(
            "owned",
            "AIX/Linux",
            resources,
            partition_id=7,
            keylock=keylock,
            max_virtual_slots=32,
            settings=settings,
        ),
    )
    assert result.settings_step.status == "ok"
    if keylock:
        create.assert_not_awaited()
        document = hmc.create_logical_partition.call_args.args[1]
        assert ">manual</KeylockPosition>" in document
        assert ">7</PartitionID>" in document
        configure.assert_awaited_once()
    else:
        hmc.create_logical_partition.assert_not_awaited()
        assert create.call_args.kwargs["partition_id"] == 7
        assert create.call_args.kwargs["settings"] == settings
        assert create.call_args.kwargs["resources"] == resources
        assert create.call_args.kwargs["max_virtual_slots"] == 32
        configure.assert_not_awaited()


@pytest.mark.asyncio
async def test_settings_failure_stops_creation_assignments(monkeypatch):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar.assignments import LparPcieAssignments
    from hmcpctl.operations.lpar.core import LparCreation, LparCreationResult
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
    from hmcpctl.operations.lpar.workflows import create_lpar

    module = "hmcpctl.operations.lpar.workflows."
    monkeypatch.setattr(module + "prevalidate_lpar_pcie_assignments", AsyncMock())
    step = WorkflowStep("settings", "error", "readback-unavailable")
    monkeypatch.setattr(
        module + "create_and_stamp_lpar",
        AsyncMock(
            return_value=LparCreationResult(
                True, {"UUID": "created"}, True, (), settings_step=step
            )
        ),
    )
    assignments = AsyncMock()
    monkeypatch.setattr(module + "apply_validated_lpar_pcie_assignments", assignments)
    result = await create_lpar(
        object(),
        "s",
        LparCreation("l", "AIX/Linux", LparResources()),
        LparPcieAssignments(),
    )
    assert result.workflow_completed is False
    assert result.resource_created is True
    assert result.steps[-1] == step
    assignments.assert_not_awaited()


def test_settings_mcp_decoding_is_strict():
    from pydantic import TypeAdapter, ValidationError

    adapter = TypeAdapter(LparSettings)
    for value in (
        {"allow_perf_collection": 0},
        {"lpar_avail_priority": True},
        {"extra": False},
    ):
        with pytest.raises(ValidationError):
            adapter.validate_python(value)
    assert adapter.validate_python({"time_ref": False}).time_ref is False


@pytest.mark.asyncio
async def test_create_cli_serializes_settings_and_id_and_marks_unknown_failure(
    monkeypatch,
):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.ssh.lpar import create_lpar_via_cli

    run = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    await create_lpar_via_cli(
        CONFIG,
        "s",
        "l",
        resources=LparResources(desired_memory=4096),
        partition_id=7,
        settings=LparSettings(False, 0, False, True, False),
    )
    record = shlex.split(run.await_args.args[1])[-1]
    assert "lpar_id=7" in record
    assert (
        "allow_perf_collection=0,lpar_avail_priority=0,time_ref=0,simplified_remote_restart_capable=1,migration_disabled=0"
        in record
    )
    run.side_effect = HMCCLIError("connection lost")
    with pytest.raises(HMCCLIError, match="Creation outcome unknown"):
        await create_lpar_via_cli(
            CONFIG, "s", "l", settings=LparSettings(time_ref=False)
        )


@pytest.mark.asyncio
async def test_baseline_read_failure_prevents_settings_write(operation_boundary):
    b = operation_boundary
    b.read.side_effect = HMCCLIError("not an applicable partition")
    with pytest.raises(HMCCLIError):
        await configure_lpar_settings(
            b.hmc, "l", LparSettings(time_ref=False), system_name_or_uuid="s"
        )
    b.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_resources_settings_creation_preserves_max_slots(monkeypatch):
    from hmcpctl.ssh.lpar import create_lpar_via_cli

    run = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    await create_lpar_via_cli(
        CONFIG, "s", "l", max_virtual_slots=32, settings=LparSettings(time_ref=False)
    )
    record = shlex.split(run.await_args.args[1])[-1]
    assert "all_resources=1" in record
    assert "max_virtual_slots=32" in record
    assert "time_ref=0" in record
