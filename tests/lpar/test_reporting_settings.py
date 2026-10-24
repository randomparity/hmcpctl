"""C02 context, input and partition postcondition contracts."""

import shlex
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar import LparReportingSettings
from hmcpctl.operations.lpar.configuration import configure_lpar_reporting_settings
from hmcpctl.ssh.lpar import get_lpar_reporting_settings, set_lpar_reporting_settings
from hmcpctl.ssh.transport import HMCCLIError

CONFIG = HMCConfig.from_mapping({})
BASE = LparReportingSettings("a", False, "none", False)


def test_model_omission_and_explicit_values():
    assert LparReportingSettings().pairs() == []
    assert LparReportingSettings(
        work_group_id=0, electronic_err_reporting=False
    ).pairs() == [("work_group_id", 0), ("electronic_err_reporting", 0)]
    assert LparReportingSettings(work_group_id="none").pairs() == [
        ("work_group_id", "none")
    ]
    for value in (-1, 999999999999):
        assert LparReportingSettings(work_group_id=value).work_group_id == value


@pytest.mark.parametrize(
    "field,value",
    [
        ("ipl_source", "A"),
        ("ipl_source", 1),
        ("os400_restricted_io_mode", 0),
        ("electronic_err_reporting", "0"),
        ("redundant_err_path_reporting", 1),
        ("work_group_id", True),
        ("work_group_id", "1"),
        ("work_group_id", "none,name=x"),
        ("work_group_id", 1.5),
    ],
)
def test_model_strict(field, value):
    with pytest.raises((ValueError, TypeError)):
        LparReportingSettings(**{field: value})
    with pytest.raises(ValidationError):
        TypeAdapter(LparReportingSettings).validate_python({field: value})


def test_context_and_partition_admission():
    with pytest.raises(ValueError, match="ipl_source"):
        LparReportingSettings(ipl_source="a").validate_context("create", "OS400")
    with pytest.raises(ValueError, match="redundant_err_path_reporting"):
        LparReportingSettings(redundant_err_path_reporting=False).validate_context(
            "update"
        )
    for field in ("ipl_source", "os400_restricted_io_mode", "electronic_err_reporting"):
        with pytest.raises(ValueError, match="IBM i"):
            LparReportingSettings(
                **{field: "a" if field == "ipl_source" else False}
            ).validate_context("update", "AIX/Linux")
    LparReportingSettings(redundant_err_path_reporting=False).validate_context(
        "create", "AIX/Linux"
    )
    LparReportingSettings(work_group_id=0).validate_context("update", "AIX/Linux")
    with pytest.raises(ValidationError):
        TypeAdapter(LparReportingSettings).validate_python({"unknown": False})


@pytest.mark.asyncio
async def test_reader_typed_context_and_encoding(monkeypatch):
    run = AsyncMock(side_effect=["os400\n", "os400,a,0,none,1\n"])
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    assert await get_lpar_reporting_settings(
        CONFIG, "system;quoted", "owned;name", require_ibmi=True
    ) == replace(BASE, electronic_err_reporting=True)
    assert (
        shlex.split(run.await_args.args[1])[-1]
        == "lpar_env,ipl_source,os400_restricted_io_mode,work_group_id,electronic_err_reporting"
    )
    run.reset_mock(side_effect=True)
    run.return_value = ""
    await set_lpar_reporting_settings(
        CONFIG,
        "s;q",
        "l;q",
        LparReportingSettings(work_group_id="none", electronic_err_reporting=False),
    )
    assert shlex.split(run.await_args.args[1]) == [
        "chsyscfg",
        "-r",
        "lpar",
        "-m",
        "s;q",
        "-i",
        "name=l;q,work_group_id=none,electronic_err_reporting=0",
    ]
    with pytest.raises(HMCCLIError):
        await set_lpar_reporting_settings(
            CONFIG, "s", "bad,name", LparReportingSettings(work_group_id=0)
        )
    assert run.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "os400,a,2,none,0",
        "os400,a,0,bad,0",
        "os400,A,0,none,0",
        "os400,a,0,none,0,extra",
        "aixlinux,a,0,none,0",
        "os400,a,0,none,0\nos400,a,0,none,0",
    ],
)
async def test_reader_refuses_incomplete_malformed_changed_environment(
    monkeypatch, raw
):
    run = AsyncMock(side_effect=["os400", raw])
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    with pytest.raises(HMCCLIError):
        await get_lpar_reporting_settings(CONFIG, "s", "l")


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["", "vioserver", "os400\nos400", "aixlinux"])
async def test_ibmi_reader_refuses_missing_ambiguous_inapplicable(
    monkeypatch, environment
):
    run = AsyncMock(return_value=environment)
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    with pytest.raises(HMCCLIError):
        await get_lpar_reporting_settings(CONFIG, "s", "l", require_ibmi=True)
    assert run.await_count == 1


@pytest.mark.asyncio
async def test_common_reader_does_not_request_ibmi_fields(monkeypatch):
    run = AsyncMock(side_effect=["aixlinux", "aixlinux,none"])
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    assert await get_lpar_reporting_settings(CONFIG, "s", "l") == LparReportingSettings(
        work_group_id="none"
    )
    assert shlex.split(run.await_args.args[1])[-1] == "lpar_env,work_group_id"


@pytest.fixture
def boundary(monkeypatch):
    b = SimpleNamespace(
        hmc=SimpleNamespace(config=CONFIG),
        resolve=AsyncMock(return_value=("s", "l")),
        read=AsyncMock(return_value=BASE),
        write=AsyncMock(),
    )
    for name, mock in [
        ("resolve_and_authorize_lpar_names", b.resolve),
        ("get_lpar_reporting_settings", b.read),
        ("set_lpar_reporting_settings", b.write),
    ]:
        monkeypatch.setattr("hmcpctl.operations.lpar.configuration." + name, mock)
    return b


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
async def test_update_postconditions(boundary, mode, status):
    b = boundary
    after = replace(BASE, ipl_source="b")
    if mode == "mismatch":
        after = BASE
    if mode == "collateral":
        after = replace(after, work_group_id=9)
    b.read.side_effect = [
        BASE,
        HMCCLIError("read unavailable") if mode == "read" else after,
    ]
    if mode == "write":
        b.write.side_effect = HMCCLIError("lost reply")
    result = await configure_lpar_reporting_settings(
        b.hmc, "l", LparReportingSettings(ipl_source="b"), system_name_or_uuid="s"
    )
    assert result.status == status
    assert result.requested.ipl_source == "b"
    b.write.assert_awaited_once()
    assert b.read.await_count == (1 if mode == "write" else 2)
    b.resolve.assert_awaited_once_with(
        b.hmc, "l", system_name_or_uuid="s", ownership_override=False
    )


@pytest.mark.asyncio
async def test_refusals_precede_effects(boundary):
    b = boundary
    for settings in [
        LparReportingSettings(),
        LparReportingSettings(redundant_err_path_reporting=False),
    ]:
        with pytest.raises(ValueError):
            await configure_lpar_reporting_settings(
                b.hmc, "l", settings, system_name_or_uuid="s"
            )
    b.resolve.assert_not_awaited()
    b.resolve.side_effect = PermissionError("foreign owner")
    with pytest.raises(PermissionError):
        await configure_lpar_reporting_settings(
            b.hmc, "l", BASE, system_name_or_uuid="s"
        )
    b.read.assert_not_awaited()
    b.write.assert_not_awaited()
    b.resolve.side_effect = None
    b.read.side_effect = HMCCLIError("inapplicable")
    with pytest.raises(HMCCLIError):
        await configure_lpar_reporting_settings(
            b.hmc, "l", BASE, system_name_or_uuid="s"
        )
    b.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings,keylock,ptype",
    [
        (LparReportingSettings(ipl_source="a"), None, "OS400"),
        (LparReportingSettings(electronic_err_reporting=False), None, "AIX/Linux"),
        (LparReportingSettings(work_group_id=0), "manual", "AIX/Linux"),
    ],
)
async def test_create_conflicts_refuse_before_resolution(settings, keylock, ptype):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar.core import LparCreation, create_and_stamp_lpar

    hmc = SimpleNamespace(config=CONFIG)
    with pytest.raises(ValueError):
        await create_and_stamp_lpar(
            hmc,
            "s",
            LparCreation(
                "l",
                ptype,
                LparResources(),
                keylock=keylock,
                reporting_settings=settings,
            ),
        )


@pytest.mark.asyncio
async def test_create_transport_passes_four_fields_and_preserves_c01_and_io(
    monkeypatch,
):
    from hmcpctl.documents.lpar import LparResources, LparSettings
    from hmcpctl.documents.lpar_io import LparPhysicalIO
    from hmcpctl.ssh.lpar import create_lpar_via_cli

    run = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.lpar.run_hmc_command", run)
    await create_lpar_via_cli(
        CONFIG,
        "s",
        "l",
        "OS400",
        LparResources(desired_memory=4096),
        32,
        partition_id=7,
        settings=LparSettings(time_ref=False),
        physical_io=LparPhysicalIO(console_slot="hmc"),
        reporting_settings=LparReportingSettings(
            os400_restricted_io_mode=False,
            work_group_id="none",
            electronic_err_reporting=False,
            redundant_err_path_reporting=True,
        ),
    )
    record = shlex.split(run.await_args.args[1])[-1]
    for part in (
        "lpar_id=7",
        "console_slot=hmc",
        "max_virtual_slots=32",
        "time_ref=0",
        "os400_restricted_io_mode=0",
        "work_group_id=none",
        "electronic_err_reporting=0",
        "redundant_err_path_reporting=1",
    ):
        assert part in record
    assert "ipl_source" not in record


@pytest.mark.asyncio
@pytest.mark.parametrize("readback", ["ok", "mismatch", "unavailable"])
async def test_creation_separate_readback_step(monkeypatch, readback):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar.core import LparCreation, create_and_stamp_lpar

    settings = LparReportingSettings(
        work_group_id="none", redundant_err_path_reporting=False
    )
    module = "hmcpctl.operations.lpar.core."
    hmc = SimpleNamespace(
        config=CONFIG,
        find_partition_by_name=AsyncMock(side_effect=[None, {"UUID": "created"}]),
        create_logical_partition=AsyncMock(),
    )
    for name, mock in [
        ("resolve_system_uuid", AsyncMock(return_value="system-uuid")),
        ("require_memory_within_system", AsyncMock()),
        ("resolve_system_cli_name", AsyncMock(return_value="s")),
        ("stamp_created_lpar_ownership", AsyncMock(return_value=(True, []))),
        ("create_lpar_via_cli", AsyncMock()),
    ]:
        monkeypatch.setattr(module + name, mock)
    read = AsyncMock(
        return_value=settings
        if readback == "ok"
        else replace(settings, work_group_id=9)
    )
    if readback == "unavailable":
        read.side_effect = HMCCLIError("no readback")
    monkeypatch.setattr(module + "get_lpar_reporting_settings", read)
    result = await create_and_stamp_lpar(
        hmc,
        "s",
        LparCreation(
            "l",
            "AIX/Linux",
            LparResources(desired_memory=4096),
            partition_id=7,
            max_virtual_slots=32,
            reporting_settings=settings,
        ),
    )
    assert result.resource_created is True
    assert result.reporting_step.status == ("ok" if readback == "ok" else "error")
    hmc.create_logical_partition.assert_not_awaited()
    read.assert_awaited_once_with(CONFIG, "s", "l", creation=True, require_ibmi=False)


@pytest.mark.asyncio
async def test_reporting_failure_stops_assignments(monkeypatch):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar.assignments import LparPcieAssignments
    from hmcpctl.operations.lpar.core import LparCreation, LparCreationResult
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
    from hmcpctl.operations.lpar.workflows import create_lpar

    module = "hmcpctl.operations.lpar.workflows."
    monkeypatch.setattr(module + "prevalidate_lpar_pcie_assignments", AsyncMock())
    step = WorkflowStep("reporting_settings", "error", "unverified")
    monkeypatch.setattr(
        module + "create_and_stamp_lpar",
        AsyncMock(
            return_value=LparCreationResult(
                True, {"UUID": "created"}, True, (), reporting_step=step
            )
        ),
    )
    assign = AsyncMock()
    monkeypatch.setattr(module + "apply_validated_lpar_pcie_assignments", assign)
    result = await create_lpar(
        object(),
        "s",
        LparCreation("l", "AIX/Linux", LparResources()),
        LparPcieAssignments(),
    )
    assert result.workflow_completed is False
    assert result.steps[-1] == step
    assign.assert_not_awaited()


@pytest.mark.parametrize("corpus", ["p10", "p11"])
@pytest.mark.parametrize(
    "context,field,value",
    [
        ("create", "os400_restricted_io_mode", False),
        ("create", "work_group_id", "none"),
        ("create", "electronic_err_reporting", False),
        ("create", "redundant_err_path_reporting", False),
        ("update", "ipl_source", "d"),
        ("update", "os400_restricted_io_mode", False),
        ("update", "work_group_id", "none"),
        ("update", "electronic_err_reporting", False),
    ],
)
def test_sixteen_reference_partition_bindings(corpus, context, field, value):
    # #1763 selectors bind these exact partition contexts in each command corpus;
    # chsyscfg's later redundant-reporting profile occurrence is not an update.
    settings = LparReportingSettings(**{field: value})
    settings.validate_context(context, "OS400")
    assert settings.pairs() == [(field, int(value) if type(value) is bool else value)]
