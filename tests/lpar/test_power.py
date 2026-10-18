"""Tests for managed-system, VIOS, and LPAR power jobs."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from conftest import JOB_ENTRY, make_config

from hmcpctl.audit import sink as audit_sink
from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError
from hmcpctl.jobs import (
    BOOT_MODES,
    POWER_OFF_OPERATIONS,
    POWER_ON_KEYLOCKS,
    POWER_ON_OPERATION_TYPES,
    power_off_lpar_job,
    power_off_system_job,
    power_off_vios_job,
    power_on_lpar_job,
    power_on_system_job,
    power_on_vios_job,
)
from hmcpctl.operations.lpar import decommission
from hmcpctl.operations.lpar.core import (
    LparPowerResult,
    _unapplied_activation_clause,
    activation_allows_assessment,
    power_off_lpar,
    power_on_lpar,
    power_on_outcome,
    submit_power_on_lpar,
)
from hmcpctl.operations.partition_state import PARTITION_STATES

SYSTEM_UUID = "00000000-0000-0000-0000-000000000001"
VIOS_UUID = "00000000-0000-0000-0000-000000000003"
PROFILE_UUID = "00000000-0000-0000-0000-0000000000aa"

# Captured from the builder before this change, so the byte-identity promise in
# ADR 0161 is pinned against a recorded value rather than the current code.
POWER_ON_LPAR_DEFAULT_DOCUMENT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<JobRequest xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/"'
    ' xmlns:JobRequest="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/"'
    ' schemaVersion="V1_0">\n'
    "  <Metadata><Atom/></Metadata>\n"
    '  <RequestedOperation kb="CUR" kxe="false" schemaVersion="V1_0">\n'
    "    <Metadata><Atom/></Metadata>\n"
    '    <OperationName kb="ROR" kxe="false">PowerOn</OperationName>\n'
    '    <GroupName kb="ROR" kxe="false">LogicalPartition</GroupName>\n'
    '    <ProgressType kb="ROR" kxe="false">DISCRETE</ProgressType>\n'
    "  </RequestedOperation>\n"
    '  <JobParameters kb="CUR" kxe="false" schemaVersion="V1_0">\n'
    "    <Metadata><Atom/></Metadata>\n"
    '    <JobParameter schemaVersion="V1_0">\n'
    "      <Metadata><Atom/></Metadata>\n"
    '      <ParameterName kb="ROR" kxe="false">force</ParameterName>\n'
    '      <ParameterValue kb="CUR" kxe="false">false</ParameterValue>\n'
    "    </JobParameter>\n"
    '    <JobParameter schemaVersion="V1_0">\n'
    "      <Metadata><Atom/></Metadata>\n"
    '      <ParameterName kb="ROR" kxe="false">novsi</ParameterName>\n'
    '      <ParameterValue kb="CUR" kxe="false">true</ParameterValue>\n'
    "    </JobParameter>\n"
    '    <JobParameter schemaVersion="V1_0">\n'
    "      <Metadata><Atom/></Metadata>\n"
    '      <ParameterName kb="ROR" kxe="false">bootmode</ParameterName>\n'
    '      <ParameterValue kb="CUR" kxe="false">norm</ParameterValue>\n'
    "    </JobParameter>\n"
    "  </JobParameters>\n"
    "</JobRequest>\n"
)

# Captured from the builder before this change, so ADR 0164's byte-identity
# promise is pinned against a recorded value rather than the current code.
POWER_OFF_LPAR_DEFAULT_DOCUMENT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<JobRequest xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/"'
    ' xmlns:JobRequest="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/"'
    ' schemaVersion="V1_0">\n'
    "  <Metadata><Atom/></Metadata>\n"
    '  <RequestedOperation kb="CUR" kxe="false" schemaVersion="V1_0">\n'
    "    <Metadata><Atom/></Metadata>\n"
    '    <OperationName kb="ROR" kxe="false">PowerOff</OperationName>\n'
    '    <GroupName kb="ROR" kxe="false">LogicalPartition</GroupName>\n'
    '    <ProgressType kb="ROR" kxe="false">DISCRETE</ProgressType>\n'
    "  </RequestedOperation>\n"
    '  <JobParameters kb="CUR" kxe="false" schemaVersion="V1_0">\n'
    "    <Metadata><Atom/></Metadata>\n"
    '    <JobParameter schemaVersion="V1_0">\n'
    "      <Metadata><Atom/></Metadata>\n"
    '      <ParameterName kb="ROR" kxe="false">immediate</ParameterName>\n'
    '      <ParameterValue kb="CUR" kxe="false">false</ParameterValue>\n'
    "    </JobParameter>\n"
    '    <JobParameter schemaVersion="V1_0">\n'
    "      <Metadata><Atom/></Metadata>\n"
    '      <ParameterName kb="ROR" kxe="false">restart</ParameterName>\n'
    '      <ParameterValue kb="CUR" kxe="false">false</ParameterValue>\n'
    "    </JobParameter>\n"
    '    <JobParameter schemaVersion="V1_0">\n'
    "      <Metadata><Atom/></Metadata>\n"
    '      <ParameterName kb="ROR" kxe="false">operation</ParameterName>\n'
    '      <ParameterValue kb="CUR" kxe="false">shutdown</ParameterValue>\n'
    "    </JobParameter>\n"
    "  </JobParameters>\n"
    "</JobRequest>\n"
)


def test_system_power_jobs():
    assert (
        "PowerOn" in power_on_system_job() and "ManagedSystem" in power_on_system_job()
    )
    assert (
        "PowerOff" in power_off_system_job()
        and "ManagedSystem" in power_off_system_job()
    )


def test_vios_power_jobs():
    on = power_on_vios_job()
    off = power_off_vios_job()
    assert "PowerOn" in on and "VirtualIOServer" in on
    assert "PowerOff" in off and "VirtualIOServer" in off


def test_power_on_lpar_job_default_document_is_unchanged():
    """A call passing no activation argument emits today's document exactly."""
    assert power_on_lpar_job() == POWER_ON_LPAR_DEFAULT_DOCUMENT


def _parameter_values(document: str, name: str) -> list[str]:
    """Every ParameterValue whose ParameterName is *name*, in document order."""
    values = []
    blocks = document.split('<JobParameter schemaVersion="V1_0">')[1:]
    for block in blocks:
        parameter = block.split('<ParameterName kb="ROR" kxe="false">')[1]
        parameter_name, _, rest = parameter.partition("</ParameterName>")
        if parameter_name == name:
            value = rest.split('<ParameterValue kb="CUR" kxe="false">')[1]
            values.append(value.partition("</ParameterValue>")[0])
    return values


def test_power_on_lpar_job_emits_optional_parameters_when_supplied():
    """LogicalPartitionProfile and keylock appear only when asked for."""
    supplied = power_on_lpar_job(
        profile_uuid=PROFILE_UUID,
        bootmode="sms",
        keylock="manual",
    )
    assert _parameter_values(supplied, "LogicalPartitionProfile") == [PROFILE_UUID]
    assert _parameter_values(supplied, "bootmode") == ["sms"]
    assert _parameter_values(supplied, "keylock") == ["manual"]

    for omitted in (power_on_lpar_job(), power_on_lpar_job(profile_uuid="")):
        assert _parameter_values(omitted, "LogicalPartitionProfile") == []
        assert _parameter_values(omitted, "OperationType") == []
        assert _parameter_values(omitted, "keylock") == []
        assert _parameter_values(omitted, "bootmode") == ["norm"]


def test_power_on_lpar_job_never_sends_operation_type_for_activate():
    """``activate`` is the HMC's default, and V10R3 refuses the parameter itself.

    The live lpar-power arm saw PowerOn end FAILED_TO_START with "Parameter
    'OperationType' is not allowed for this Job." (#1392), so stating the default
    explicitly must emit the same document as omitting it.
    """
    for kwargs in ({}, {"profile_uuid": PROFILE_UUID}, {"keylock": "norm"}):
        explicit = power_on_lpar_job(operation_type="activate", **kwargs)
        assert _parameter_values(explicit, "OperationType") == []
        assert explicit == power_on_lpar_job(**kwargs)


@pytest.mark.parametrize("keylock", sorted(POWER_ON_KEYLOCKS))
def test_power_on_lpar_job_accepts_every_keylock_position(keylock):
    """Both documented job keylock positions reach the document unaltered."""
    document = power_on_lpar_job(keylock=keylock)
    assert _parameter_values(document, "keylock") == [keylock]


@pytest.mark.parametrize("boot_mode", sorted(BOOT_MODES))
def test_power_on_lpar_job_accepts_every_boot_mode(boot_mode):
    """Every documented boot mode reaches the document unaltered."""
    assert _parameter_values(power_on_lpar_job(bootmode=boot_mode), "bootmode") == [
        boot_mode
    ]


@pytest.mark.parametrize(
    ("kwargs", "permitted"),
    [
        ({"bootmode": "warp"}, BOOT_MODES),
        ({"operation_type": "netboot"}, POWER_ON_OPERATION_TYPES),
        ({"operation_type": ""}, POWER_ON_OPERATION_TYPES),
        # The creation-time Keylock spelling is not the job's (ADR 0161 amendment).
        ({"keylock": "normal"}, POWER_ON_KEYLOCKS),
        ({"keylock": "auto"}, POWER_ON_KEYLOCKS),
        ({"keylock": ""}, POWER_ON_KEYLOCKS),
    ],
)
def test_power_on_lpar_job_rejects_unknown_vocabulary(kwargs, permitted):
    """A non-member is refused before XML exists, naming the sorted permitted set."""
    with pytest.raises(ValueError) as rejected:
        power_on_lpar_job(**kwargs)
    assert ", ".join(sorted(permitted)) in str(rejected.value)


def test_power_off_lpar_job_default_document_is_unchanged():
    """A call passing no new argument emits today's document exactly."""
    assert power_off_lpar_job() == POWER_OFF_LPAR_DEFAULT_DOCUMENT


@pytest.mark.parametrize(
    ("immediate", "restart"),
    [(False, False), (True, False), (True, True)],
)
def test_power_off_lpar_job_emits_restart_and_operation(immediate, restart):
    """All three parameters are emitted on every call, in the document's order."""
    document = power_off_lpar_job(immediate=immediate, restart=restart)
    assert _parameter_values(document, "immediate") == [
        "true" if immediate else "false"
    ]
    assert _parameter_values(document, "restart") == ["true" if restart else "false"]
    assert _parameter_values(document, "operation") == ["shutdown"]
    assert _parameter_values(
        power_off_lpar_job(operation="osshutdown"), "operation"
    ) == ["osshutdown"]


@pytest.mark.parametrize("operation", ["dumpretry", "", "reboot", "SHUTDOWN"])
def test_power_off_lpar_job_rejects_unknown_vocabulary(operation):
    """A non-member is refused before XML exists, naming the sorted permitted set."""
    with pytest.raises(ValueError) as rejected:
        power_off_lpar_job(operation=operation)
    assert ", ".join(sorted(POWER_OFF_OPERATIONS)) in str(rejected.value)


def test_power_off_lpar_job_gates_dumprestart_behind_the_opt_in():
    """dumprestart crashes the partition, so it is refused without the opt-in."""
    with pytest.raises(ValueError) as refused:
        power_off_lpar_job(operation="dumprestart")
    assert "allow_dump_restart" in str(refused.value)

    permitted = power_off_lpar_job(operation="dumprestart", allow_dump_restart=True)
    assert _parameter_values(permitted, "operation") == ["dumprestart"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"restart": True},
        {"immediate": False, "restart": True, "operation": "shutdown"},
    ],
)
def test_power_off_lpar_job_refuses_the_implicit_dump_restart(kwargs):
    """shutdown + restart without immediate is the HMC's dump restart, so it is refused.

    The message names every way to ask for what the caller probably meant.
    """
    with pytest.raises(ValueError) as refused:
        power_off_lpar_job(**kwargs)
    message = str(refused.value)
    assert "immediate=true" in message
    assert "osshutdown" in message
    assert "hmc_dump_restart_lpar" in message


@pytest.mark.parametrize(
    "kwargs",
    [
        {"immediate": True, "restart": True},
        {"restart": True, "operation": "osshutdown"},
        {"restart": True, "operation": "dumprestart", "allow_dump_restart": True},
    ],
)
def test_power_off_lpar_job_still_builds_every_other_restart(kwargs):
    """The refusal is scoped to shutdown without immediate; other restarts build."""
    document = power_off_lpar_job(**kwargs)
    assert _parameter_values(document, "restart") == ["true"]


@pytest.mark.asyncio
async def test_power_on_system(mock_hmc):
    route = mock_hmc.put(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/do/PowerOn").mock(
        return_value=httpx.Response(202, text=JOB_ENTRY)
    )
    async with HMCClient(make_config()) as hmc:
        job = await hmc.power_on_system(SYSTEM_UUID)
    assert route.called
    assert job is not None


@pytest.mark.asyncio
async def test_power_off_system(mock_hmc):
    route = mock_hmc.put(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/do/PowerOff").mock(
        return_value=httpx.Response(202, text=JOB_ENTRY)
    )
    async with HMCClient(make_config()) as hmc:
        await hmc.power_off_system(SYSTEM_UUID, immediate=True)
    body = route.calls.last.request.content.decode()
    assert "PowerOff" in body and "immediate" in body


@pytest.mark.asyncio
async def test_power_on_vios(mock_hmc):
    route = mock_hmc.put(f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/do/PowerOn").mock(
        return_value=httpx.Response(202, text=JOB_ENTRY)
    )
    async with HMCClient(make_config()) as hmc:
        await hmc.power_on_vios(VIOS_UUID)
    assert route.called


@pytest.mark.asyncio
async def test_power_off_vios(mock_hmc):
    route = mock_hmc.put(f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/do/PowerOff").mock(
        return_value=httpx.Response(202, text=JOB_ENTRY)
    )
    async with HMCClient(make_config()) as hmc:
        await hmc.power_off_vios(VIOS_UUID)
    assert route.called


# --------------------------------------------------------------------------- #
# The activation parameters reaching the job document through submit_power_on_lpar
# --------------------------------------------------------------------------- #

LPAR_UUID = "11111111-1111-1111-1111-111111111111"


def _power_client() -> AsyncMock:
    """A client double whose config is real, so the ADR 0092 guard stays off.

    ``AsyncMock().config.authorize_power_operations`` is a truthy child mock,
    which would silently enable the ownership guard and change the call path.
    """
    hmc = AsyncMock()
    hmc.config = HMCConfig.from_mapping(
        {"host": "hmc.test", "user": "u", "password": "p"}
    )
    hmc.get_quick_property.return_value = "not activated"
    hmc.submit_job.return_value = {"UUID": "job-uuid"}
    # ADR 0039 containment: the target partition contains PROFILE_UUID, so a
    # call naming it passes the check rather than tripping it.
    hmc.list_child.return_value = [{"UUID": PROFILE_UUID}]
    return hmc


@pytest.mark.asyncio
async def test_power_operation_forwards_activation_parameters():
    """PowerOn carries the caller's profile, boot mode and keylock, never activate."""
    hmc = _power_client()

    with patch(
        "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
        new=AsyncMock(return_value=LPAR_UUID),
    ):
        await submit_power_on_lpar(
            hmc,
            LPAR_UUID,
            boot_mode="sms",
            partition_profile_uuid=PROFILE_UUID,
            operation_type="activate",
            keylock="norm",
        )

    _, document = hmc.submit_job.await_args.args
    assert _parameter_values(document, "bootmode") == ["sms"]
    assert _parameter_values(document, "LogicalPartitionProfile") == [PROFILE_UUID]
    assert _parameter_values(document, "OperationType") == []
    assert _parameter_values(document, "keylock") == ["norm"]


@pytest.mark.asyncio
async def test_power_operation_forwards_power_off_parameters():
    """PowerOff carries the caller's restart flag and shutdown operation."""
    hmc = _power_client()

    with patch(
        "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
        new=AsyncMock(return_value=LPAR_UUID),
    ):
        await power_off_lpar(hmc, LPAR_UUID, restart=True, operation="osshutdown")

    path, document = hmc.submit_job.await_args.args
    assert path.endswith("/do/PowerOff")
    assert _parameter_values(document, "restart") == ["true"]
    assert _parameter_values(document, "operation") == ["osshutdown"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"operation": "dumpretry"}, "dumprestart, osshutdown, shutdown"),
        ({"operation": "dumprestart"}, "allow_dump_restart"),
        ({"restart": True}, "hmc_dump_restart_lpar"),
    ],
)
async def test_power_operation_refuses_before_any_side_effect(kwargs, expected, caplog):
    """A refused PowerOff reads nothing, submits nothing and audits nothing."""
    hmc = _power_client()
    resolver = AsyncMock(return_value=LPAR_UUID)

    with (
        patch("hmcpctl.operations.lpar.core.resolve_lpar_uuid", new=resolver),
        caplog.at_level(logging.INFO),
        pytest.raises(ValueError) as refused,
    ):
        await power_off_lpar(hmc, LPAR_UUID, **kwargs)

    assert expected in str(refused.value)
    resolver.assert_not_awaited()
    hmc.submit_job.assert_not_awaited()
    hmc.get_quick_property.assert_not_awaited()
    assert _audit_records(caplog) == []


def _audit_records(caplog) -> list[dict]:
    """Every record the reserved audit logger emitted, parsed."""
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == audit_sink.AUDIT_LOGGER_NAME
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "immediate", "restart"),
    [
        ("shutdown", True, False),
        ("shutdown", True, True),
        ("osshutdown", False, False),
        ("dumprestart", False, True),
    ],
)
async def test_power_off_records_the_variant_it_sends(
    operation, immediate, restart, caplog
):
    """ADR 0180. One ``lpar-power-off`` record per PowerOff, naming what was sent.

    The record's variant fields are compared with the submitted document's own
    parameters, so the record cannot describe a call the HMC did not receive.
    """
    hmc = _power_client()
    hmc.config = HMCConfig.from_mapping(
        {"host": "hmc.test", "user": "u", "password": "p", "agent_id": "agent-7"}
    )

    with (
        patch(
            "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
            new=AsyncMock(return_value=LPAR_UUID),
        ),
        caplog.at_level(logging.INFO),
    ):
        await power_off_lpar(
            hmc,
            "lpar-a",
            immediate=immediate,
            restart=restart,
            operation=operation,
            allow_dump_restart=operation == "dumprestart",
        )

    records = _audit_records(caplog)
    assert [record["event"] for record in records] == ["lpar-power-off"]
    record = records[0]
    assert list(record) == [
        "time",
        "event",
        "lpar",
        "host",
        "operation",
        "immediate",
        "restart",
        "attribution",
    ]
    _, document = hmc.submit_job.await_args.args
    assert [record["operation"]] == _parameter_values(document, "operation")
    assert [str(record["immediate"]).lower()] == _parameter_values(
        document, "immediate"
    )
    assert [str(record["restart"]).lower()] == _parameter_values(document, "restart")
    assert (record["operation"], record["immediate"], record["restart"]) == (
        operation,
        immediate,
        restart,
    )
    assert (record["lpar"], record["host"]) == (LPAR_UUID, "hmc.test")
    assert record["attribution"] == {
        "claim": "agent-7",
        "source": "config:agent_id",
        "verified": False,
    }
    assert [
        r.levelno for r in caplog.records if r.name == audit_sink.AUDIT_LOGGER_NAME
    ] == [logging.WARNING]


@pytest.mark.asyncio
async def test_power_off_is_recorded_even_when_the_submit_raises(caplog):
    """The record precedes the submit, so a submit that raises is still recorded."""
    hmc = _power_client()
    hmc.submit_job.side_effect = HMCError("submit failed")

    with (
        patch(
            "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
            new=AsyncMock(return_value=LPAR_UUID),
        ),
        caplog.at_level(logging.INFO),
        pytest.raises(HMCError),
    ):
        await power_off_lpar(
            hmc,
            LPAR_UUID,
            operation="dumprestart",
            allow_dump_restart=True,
        )

    records = _audit_records(caplog)
    assert [(r["event"], r["operation"]) for r in records] == [
        ("lpar-power-off", "dumprestart")
    ]


@pytest.mark.asyncio
async def test_power_on_emits_no_power_off_record(caplog):
    """Power-on is not audited by this record (ADR 0180 scope)."""
    hmc = _power_client()

    with (
        patch(
            "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
            new=AsyncMock(return_value=LPAR_UUID),
        ),
        caplog.at_level(logging.INFO),
    ):
        await submit_power_on_lpar(hmc, LPAR_UUID)

    hmc.submit_job.assert_awaited_once()
    assert _audit_records(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("immediate", [False, True])
async def test_decommission_power_off_document_is_unchanged(immediate):
    """The decommission path shares the builder, so its defaults must not move.

    #872 widened power_off_lpar_job with restart and operation. This workflow
    passes neither, so it must keep emitting restart=false and
    operation=shutdown; epic #871 freezes this document. The private
    ``_power_off`` is called deliberately: the contract is exactly which
    document that call site builds.
    """
    hmc = _power_client()
    inventory = SimpleNamespace(
        state="running", lpar_uuid=LPAR_UUID, lpar_name="lpar-a"
    )

    with patch(
        "hmcpctl.operations.lpar.decommission.wait_for_submitted_job",
        new=AsyncMock(
            return_value={
                "UUID": "job-uuid",
                "Resource": {"JobID": "job-uuid", "Status": "COMPLETED_OK"},
            }
        ),
    ):
        await decommission._power_off(
            hmc,
            inventory,
            immediate=immediate,
            timeout_seconds=30,
            poll_interval=1,
        )

    path, document = hmc.submit_job.await_args.args
    assert path.endswith("/do/PowerOff")
    assert _parameter_values(document, "immediate") == [
        "true" if immediate else "false"
    ]
    assert _parameter_values(document, "restart") == ["false"]
    assert _parameter_values(document, "operation") == ["shutdown"]


@pytest.mark.asyncio
async def test_power_on_lpar_passes_activation_parameters():
    """power_on_lpar hands all three straight to the shared power entry point."""
    hmc = _power_client()
    forwarded = AsyncMock(return_value=LparPowerResult(LPAR_UUID, {"UUID": "job-uuid"}))

    with patch("hmcpctl.operations.lpar.core.submit_power_on_lpar", new=forwarded):
        await power_on_lpar(
            hmc,
            LPAR_UUID,
            boot_mode="of",
            partition_profile_uuid=PROFILE_UUID,
            operation_type="activate",
            keylock="manual",
        )

    assert forwarded.await_args.kwargs["boot_mode"] == "of"
    assert forwarded.await_args.kwargs["partition_profile_uuid"] == PROFILE_UUID
    assert forwarded.await_args.kwargs["operation_type"] == "activate"
    assert forwarded.await_args.kwargs["keylock"] == "manual"


@pytest.mark.asyncio
async def test_power_operation_already_running_names_the_dropped_activation_parameters():
    """A running partition drops the activation request, so the message says so.

    "Boot this partition into SMS" is usually asked about a running partition,
    and a bare already-running message reads as success to a caller whose
    request was never attempted.
    """
    hmc = _power_client()
    hmc.get_quick_property.return_value = "running"

    with patch(
        "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
        new=AsyncMock(return_value=LPAR_UUID),
    ):
        requested = await submit_power_on_lpar(hmc, LPAR_UUID, boot_mode="sms")
        plain = await submit_power_on_lpar(hmc, LPAR_UUID)

    hmc.submit_job.assert_not_awaited()
    assert requested.job["already_running"] is True
    # Only the parameter actually supplied is named, and force=True is not
    # offered: the HMC fails a PowerOn from any activated state (HSCL3681).
    assert "The requested boot mode was not applied" in requested.job["message"]
    for unsupplied in ("partition profile", "operation type", "keylock"):
        assert unsupplied not in requested.job["message"]
    assert "force=True" not in requested.job["message"]
    # An ordinary already-running call names no unapplied parameter.
    assert "not applied" not in plain.job["message"]


# Every PartitionState except ``not activated`` refuses PowerOn with HSCL3681, so
# none of them may reach a submit without force=True.
_ACTIVE_STATES = ("running", "starting", "open firmware")
_UNSETTLED_STATES = sorted(PARTITION_STATES - {"not activated", *_ACTIVE_STATES})

# The job entry a forced PowerOn returned from ``open firmware`` in the #1161 live
# capture: the HMC accepts the submit, then fails the job.
HSCL3681 = (
    "HSCL3681 Partition 1 cannot be activated since it is not in the "
    "Not Activated state.\n"
)
HSCL3681_JOB = {
    "UUID": "job-uuid",
    "Resource": {
        "JobID": "job-uuid",
        "Status": "COMPLETED_WITH_ERROR",
        "Results": {
            "JobParameter": [
                {"ParameterName": "returnCode", "ParameterValue": "1"},
                {"ParameterName": "result", "ParameterValue": HSCL3681},
            ]
        },
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", _ACTIVE_STATES)
async def test_power_operation_already_active_state_submits_no_job(state):
    hmc = _power_client()
    hmc.get_quick_property.return_value = state

    with patch(
        "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
        new=AsyncMock(return_value=LPAR_UUID),
    ):
        result = await submit_power_on_lpar(hmc, LPAR_UUID)

    hmc.submit_job.assert_not_awaited()
    assert result.job["already_running"] is True
    assert state in result.job["message"]
    assert "No PowerOn job was submitted." in result.job["message"]
    assert "force=True" not in result.job["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "submitted"),
    [("Running", False), ("Open Firmware", False), ("Not Activated", True)],
)
async def test_power_operation_guard_accepts_the_cli_title_case_rendering(
    state, submitted
):
    """``lssyscfg`` renders states in title case; the guard must not depend on case."""
    hmc = _power_client()
    hmc.get_quick_property.return_value = state

    with patch(
        "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
        new=AsyncMock(return_value=LPAR_UUID),
    ):
        result = await submit_power_on_lpar(hmc, LPAR_UUID)

    assert hmc.submit_job.await_count == int(submitted)
    assert result.job.get("already_running", False) is not submitted


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [*_UNSETTLED_STATES, "unrecognized", None])
async def test_power_operation_refuses_power_on_outside_not_activated(state):
    hmc = _power_client()
    hmc.get_quick_property.return_value = state

    with (
        patch(
            "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
            new=AsyncMock(return_value=LPAR_UUID),
        ),
        pytest.raises(HMCError, match=f"current state is {state!r}") as raised,
    ):
        await submit_power_on_lpar(hmc, LPAR_UUID)

    assert raised.value.status_code == 409
    hmc.submit_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_power_operation_force_submits_from_open_firmware_and_surfaces_hscl3681():
    hmc = _power_client()
    hmc.get_quick_property.return_value = "open firmware"
    hmc.wait_for_job_entry.return_value = HSCL3681_JOB

    with patch(
        "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
        new=AsyncMock(return_value=LPAR_UUID),
    ):
        result = await submit_power_on_lpar(hmc, LPAR_UUID, force=True, wait=True)

    hmc.get_quick_property.assert_not_awaited()
    hmc.submit_job.assert_awaited_once()
    outcome = power_on_outcome(result)
    assert outcome.already_running is False
    assert outcome.job == HSCL3681_JOB
    assert activation_allows_assessment(result) == (False, HSCL3681.strip())


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, ""),
        (
            {"boot_mode": "sms"},
            (
                " The requested boot mode was not applied;"
                " power the partition off first."
            ),
        ),
        (
            {"partition_profile_uuid": PROFILE_UUID, "keylock": "manual"},
            (
                " The requested partition profile and keylock position were not"
                " applied; power the partition off first."
            ),
        ),
        (
            {
                "boot_mode": "of",
                "partition_profile_uuid": PROFILE_UUID,
                "keylock": "norm",
            },
            (
                " The requested boot mode, partition profile and keylock position"
                " were not applied; power the partition off first."
            ),
        ),
        (
            {"keylock": "manual"},
            (
                " The requested keylock position was not applied;"
                " power the partition off first."
            ),
        ),
    ],
)
def test_unapplied_activation_clause_names_only_what_was_supplied(kwargs, expected):
    """The clause enumerates supplied parameters only, with matching grammar."""
    assert (
        _unapplied_activation_clause(
            kwargs.get("boot_mode", "norm"),
            kwargs.get("partition_profile_uuid"),
            kwargs.get("keylock"),
        )
        == expected
    )


# --------------------------------------------------------------------------- #
# ADR 0039 containment: a supplied profile must belong to the target partition
# --------------------------------------------------------------------------- #

OTHER_PROFILE_UUID = "00000000-0000-0000-0000-0000000000bb"


@pytest.mark.asyncio
async def test_power_operation_accepts_a_profile_contained_by_the_target_partition():
    """A profile in the partition's own feed reaches the job document."""
    hmc = _power_client()
    hmc.list_child.return_value = [{"UUID": PROFILE_UUID}, {"UUID": OTHER_PROFILE_UUID}]

    with patch(
        "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
        new=AsyncMock(return_value=LPAR_UUID),
    ):
        await submit_power_on_lpar(hmc, LPAR_UUID, partition_profile_uuid=PROFILE_UUID)

    hmc.list_child.assert_awaited_once_with(
        "LogicalPartition", LPAR_UUID, "LogicalPartitionProfile"
    )
    _, document = hmc.submit_job.await_args.args
    assert _parameter_values(document, "LogicalPartitionProfile") == [PROFILE_UUID]


@pytest.mark.asyncio
async def test_power_operation_refuses_a_profile_the_target_partition_does_not_contain():
    """ADR 0039: the tool declares exhaustive targets, so a foreign profile is refused.

    Without this the declared `lpar` selector bounds nothing here: a narrow
    ``targets = {lpar = ["A"]}`` grant would still activate A against another
    partition's profile, because no target selector is minted for this argument.
    """
    hmc = _power_client()
    hmc.list_child.return_value = [{"UUID": OTHER_PROFILE_UUID}]

    with (
        patch(
            "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
            new=AsyncMock(return_value=LPAR_UUID),
        ),
        pytest.raises(ValueError) as refused,
    ):
        await submit_power_on_lpar(hmc, LPAR_UUID, partition_profile_uuid=PROFILE_UUID)

    hmc.submit_job.assert_not_awaited()
    # The rejected value is not echoed, matching the module's other refusals.
    assert PROFILE_UUID not in str(refused.value)


@pytest.mark.asyncio
async def test_power_operation_reads_no_profile_feed_when_no_profile_is_supplied():
    """The containment read costs nothing on the ordinary PowerOn path."""
    hmc = _power_client()

    with patch(
        "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
        new=AsyncMock(return_value=LPAR_UUID),
    ):
        await submit_power_on_lpar(hmc, LPAR_UUID, boot_mode="sms")

    hmc.list_child.assert_not_awaited()
    hmc.submit_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_power_operation_matches_a_profile_uuid_case_insensitively():
    """The HMC renders UUIDs lower-case; ``is_uuid`` admits upper-case hex.

    Mirrors ``_hosts_partition`` in operations/lpar/ownership.py, the other
    place a user-supplied UUID is compared against HMC output.
    """
    hmc = _power_client()
    hmc.list_child.return_value = [{"UUID": PROFILE_UUID.lower()}]

    with patch(
        "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
        new=AsyncMock(return_value=LPAR_UUID),
    ):
        await submit_power_on_lpar(
            hmc, LPAR_UUID, partition_profile_uuid=PROFILE_UUID.upper()
        )

    # The partition's own spelling reaches the wire, not the caller's: the match
    # is casefolded, so emitting the caller's string would send a value the
    # containment check never compared.
    _, document = hmc.submit_job.await_args.args
    assert _parameter_values(document, "LogicalPartitionProfile") == [
        PROFILE_UUID.lower()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "feed", [[], [{"ResourceType": "LogicalPartitionProfile"}], [{"UUID": None}]]
)
async def test_power_operation_distinguishes_a_degraded_profile_feed_from_a_refusal(
    feed,
):
    """An empty or unparsed feed is not evidence the profile is foreign."""
    hmc = _power_client()
    hmc.list_child.return_value = feed

    with (
        patch(
            "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
            new=AsyncMock(return_value=LPAR_UUID),
        ),
        pytest.raises(ValueError) as refused,
    ):
        await submit_power_on_lpar(hmc, LPAR_UUID, partition_profile_uuid=PROFILE_UUID)

    hmc.submit_job.assert_not_awaited()
    assert "came back empty" in str(refused.value)
    assert "is not a profile of the target partition" not in str(refused.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"boot_mode": "warp"}, "PowerOn boot mode must be one of"),
        ({"keylock": "normal"}, "PowerOn keylock must be one of: manual, norm"),
    ],
)
async def test_power_operation_refuses_an_invalid_activation_value_on_the_already_running_path(
    kwargs, expected
):
    """Criterion 2's refusal must not be swallowed by the early return.

    The already-running branch returns without building a document, so the
    builder's own check never runs on that path. The refusal also precedes the
    state read, so no HMC call is made at all.
    """
    hmc = _power_client()
    hmc.get_quick_property.return_value = "running"
    resolver = AsyncMock(return_value=LPAR_UUID)

    with (
        patch("hmcpctl.operations.lpar.core.resolve_lpar_uuid", new=resolver),
        pytest.raises(ValueError, match=expected),
    ):
        await submit_power_on_lpar(hmc, LPAR_UUID, **kwargs)

    resolver.assert_not_awaited()
    hmc.get_quick_property.assert_not_awaited()
    hmc.submit_job.assert_not_awaited()


# --------------------------------------------------------------------------- #
# #981: a partition profile that lacks current adapters is warned about
# --------------------------------------------------------------------------- #


def _profile_power_client(profile_slots: list[str], current: dict) -> AsyncMock:
    hmc = _power_client()
    subclasses = [
        {"ProfileVirtualSCSIClientAdapter": {"VirtualSlotNumber": slot}}
        for slot in profile_slots
    ]
    hmc.list_child.return_value = [
        {
            "UUID": PROFILE_UUID,
            "Resource": {
                "IOConfigurationInstance": {
                    "ProfileVirtualIOAdapters": {
                        "ProfileVirtualIOAdapterSubclass": subclasses
                    }
                }
            },
        }
    ]

    async def list_adapters(lpar_uuid, adapter_type):
        return [
            {"Resource": {"VirtualSlotNumber": slot}}
            for slot in current.get(adapter_type, [])
        ]

    hmc.list_adapters.side_effect = list_adapters
    return hmc


async def _power_on_with_profile(hmc: AsyncMock) -> LparPowerResult:
    with patch(
        "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
        new=AsyncMock(return_value=LPAR_UUID),
    ):
        return await submit_power_on_lpar(
            hmc, LPAR_UUID, partition_profile_uuid=PROFILE_UUID
        )


@pytest.mark.asyncio
async def test_power_on_warns_for_each_current_adapter_the_profile_lacks():
    hmc = _profile_power_client(
        ["2"],
        {"VirtualSCSIClientAdapter": ["2", "3"], "ClientNetworkAdapter": ["4"]},
    )

    result = await _power_on_with_profile(hmc)

    assert len(result.warnings) == 2
    assert "VirtualSCSIClientAdapter in virtual slot 3" in result.warnings[0]
    assert "ClientNetworkAdapter in virtual slot 4" in result.warnings[1]
    # Warn, not refuse: the activation is still submitted with the profile.
    _, document = hmc.submit_job.await_args.args
    assert _parameter_values(document, "LogicalPartitionProfile") == [PROFILE_UUID]


@pytest.mark.asyncio
async def test_power_on_does_not_warn_when_the_profile_has_every_adapter():
    hmc = _profile_power_client(["2", "3"], {"VirtualSCSIClientAdapter": ["2", "3"]})

    result = await _power_on_with_profile(hmc)

    assert result.warnings == ()
    hmc.submit_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_power_on_submits_with_a_warning_when_an_adapter_feed_read_fails():
    hmc = _profile_power_client([], {})
    hmc.list_adapters.side_effect = HMCError("GET adapters failed", 500)

    result = await _power_on_with_profile(hmc)

    assert result.warnings == (
        "Partition profile adapter check not run: GET adapters failed (HTTP 500)",
    )
    hmc.submit_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_power_on_without_a_profile_reads_no_adapter_feed():
    hmc = _profile_power_client([], {"VirtualSCSIClientAdapter": ["2"]})

    with patch(
        "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
        new=AsyncMock(return_value=LPAR_UUID),
    ):
        result = await submit_power_on_lpar(hmc, LPAR_UUID)

    assert result.warnings == ()
    hmc.list_adapters.assert_not_awaited()


@pytest.mark.asyncio
async def test_power_on_lpar_outcome_carries_the_profile_warnings():
    hmc = _profile_power_client([], {"VirtualSCSIClientAdapter": ["2"]})

    with patch(
        "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
        new=AsyncMock(return_value=LPAR_UUID),
    ):
        outcome = await power_on_lpar(
            hmc, LPAR_UUID, partition_profile_uuid=PROFILE_UUID
        )

    assert len(outcome.warnings) == 1
    assert "virtual slot 2" in outcome.warnings[0]


def _ok_job(status: str = "COMPLETED_OK") -> dict:
    return {
        "UUID": "job-uuid",
        "Resource": {
            "JobID": "job-uuid",
            "Status": status,
            "Results": {
                "JobParameter": [
                    {"ParameterName": "bootmode", "ParameterValue": "sms"},
                    {"ParameterName": "returnCode", "ParameterValue": "0"},
                ]
            },
        },
    }


async def _waited_power_on(state, job=None, **kwargs):
    hmc = _power_client()
    hmc.get_quick_property.side_effect = ["not activated", state]
    hmc.wait_for_job_entry.return_value = job or _ok_job()
    with patch(
        "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
        new=AsyncMock(return_value=LPAR_UUID),
    ):
        return hmc, await power_on_lpar(hmc, LPAR_UUID, wait=True, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["error", "Error", "not activated"])
async def test_waited_power_on_completed_ok_but_failed_state_raises(state):
    with pytest.raises(HMCError, match="activation failed") as raised:
        await _waited_power_on(state)

    assert f"state {state!r}" in str(raised.value)
    assert "hmc_read_lpar_refcodes" in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "starting", "open firmware", "Running"])
async def test_waited_power_on_accepts_every_activated_state(state):
    hmc, outcome = await _waited_power_on(state)

    assert outcome.already_running is False
    assert outcome.job == _ok_job()
    assert hmc.get_quick_property.await_count == 2


@pytest.mark.asyncio
async def test_waited_power_on_failed_job_is_returned_without_a_state_read():
    job = _ok_job("COMPLETED_WITH_ERROR")
    hmc, outcome = await _waited_power_on("error", job)

    assert outcome.job == job
    assert hmc.get_quick_property.await_count == 1


@pytest.mark.asyncio
async def test_unwaited_power_on_reads_no_state_after_submit():
    hmc = _power_client()
    with patch(
        "hmcpctl.operations.lpar.core.resolve_lpar_uuid",
        new=AsyncMock(return_value=LPAR_UUID),
    ):
        await power_on_lpar(hmc, LPAR_UUID)

    assert hmc.get_quick_property.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [submit_power_on_lpar, power_on_lpar])
@pytest.mark.parametrize(
    "kwargs",
    [
        {"immediate": True},
        {"restart": True},
        {"operation": "shutdown"},
        {"allow_dump_restart": True},
    ],
)
async def test_activation_rejects_shutdown_options_before_side_effects(
    operation, kwargs
):
    hmc = _power_client()
    with pytest.raises(TypeError):
        await operation(hmc, LPAR_UUID, **kwargs)
    hmc.submit_job.assert_not_awaited()
    hmc.get_quick_property.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"force": True},
        {"boot_mode": "sms"},
        {"partition_profile_uuid": "profile"},
        {"operation_type": "activate"},
        {"keylock": "norm"},
    ],
)
async def test_shutdown_rejects_activation_options_before_side_effects(kwargs):
    from hmcpctl.operations.lpar import core

    hmc = _power_client()
    with pytest.raises(TypeError):
        await core.power_off_lpar(hmc, LPAR_UUID, **kwargs)
    hmc.submit_job.assert_not_awaited()
    hmc.get_quick_property.assert_not_awaited()
