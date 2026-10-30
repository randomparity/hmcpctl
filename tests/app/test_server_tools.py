"""Tool-layer tests for REST-backed and SSH-backed MCP tools.

The client layer (``HMCClient`` + respx) is covered in the domain test dirs;
these tests call the actual ``@mcp.tool`` functions so the argument-mapping
and XML-building logic in the tool bodies is exercised — the layer the
client tests skip.  REST tools run against the respx ``mock_hmc`` router
with env-configured credentials; ``hmc_run_command`` patches the SSH
boundary (``asyncssh.connect``) like the vNIC tests do.
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import asdict
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import httpx
import pytest
from conftest import (
    COMPLETED_JOB_ENTRY,
    JOB_ENTRY,
    JOB_ID,
    LPAR_RESOURCE_CONFIG,
    RUNNING_JOB_ENTRY,
    RUNNING_JOB_ID,
    live_fixture,
    live_response,
    mock_system_summaries,
    mock_system_summary,
)

from hmcpctl.documents.lpar import LparResources
from hmcpctl.errors import HMCError
from hmcpctl.operations.updates.models import (
    PlatformUpdateParameter,
    SystemFirmwareUpdateModel,
)
from hmcpctl.server_tools.command import hmc_run_command
from hmcpctl.server_tools.jobs import (
    hmc_get_job,
    hmc_wait_for_job,
)
from hmcpctl.server_tools.lpar.lifecycle import (
    hmc_delete_lpar,
    hmc_dump_restart_lpar,
    hmc_modify_lpar,
    hmc_power_off_lpar,
    hmc_power_on_lpar,
    hmc_rename_lpar,
)
from hmcpctl.server_tools.lpar.lifecycle_create import hmc_create_lpar
from hmcpctl.server_tools.systems.core import hmc_get_lpar
from hmcpctl.server_tools.updates import (
    hmc_submit_available_hmc_ptfs_query,
    hmc_update_console_software,
    hmc_update_firmware,
    hmc_vios_update,
    hmc_vios_upgrade,
)

SYSTEM_UUID = "00000000-0000-0000-0000-000000000001"
LPAR_UUID = "00000000-0000-0000-0000-000000000002"
PARTITION_PROFILE_UUID = "00000000-0000-0000-0000-0000000000aa"

# One-profile Atom feed for the partition's LogicalPartitionProfile children.
PARTITION_PROFILE_FEED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:{uuid}</id>
    <title>LogicalPartitionProfile:default</title>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <LogicalPartitionProfile xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <ProfileName>default</ProfileName>
      </LogicalPartitionProfile>
    </content>
  </entry>
</feed>
"""
VIOS_UUID = "00000000-0000-0000-0000-000000000003"
MC_UUID = "mc-uuid-0001"
CONSOLE_SOURCE = {
    "MediaType": "NFS",
    "ServerHostOrIP": "repo.example.com",
    "Directory": "/images/hmc",
    "RestartConsole": "False",
}
VIOS_UPDATE_SOURCE = {
    "ResourceType": "NFS",
    "ServerHostOrIP": "repo.example.com",
    "RemoteDirectory": "/images/vios",
}
VIOS_UPGRADE_SOURCE = {
    "ResourceType": "NFS",
    "ServerHostOrIP": "repo.example.com",
    "RemoteDirectory": "/images/vios",
    "Disks": "hdisk1",
}
PLATFORM_UPDATE = PlatformUpdateParameter(
    SystemFirmwareUpdate=SystemFirmwareUpdateModel(UpdateType="Update", UpdateOrder=1)
)


def _console_feed(version: tuple[str, str, str] | None) -> str:
    """A console feed whose ``VersionInfo`` is nested as V10R3 serves it (#1202)."""
    version_xml = (
        "<VersionInfo><Version>{}</Version><Release>{}</Release>"
        "<ServicePackName>{}</ServicePackName></VersionInfo>".format(*version)
        if version is not None
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:{MC_UUID}</id>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <ManagementConsole xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        {version_xml}
      </ManagementConsole>
    </content>
  </entry>
</feed>"""


# A single-LPAR Atom feed; {name} is the partition name.
LPAR_FEED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:00000000-0000-0000-0000-000000000002</id>
    <title>LogicalPartition</title>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <PartitionName>{name}</PartitionName>
        <PartitionState>running</PartitionState>
      </LogicalPartition>
    </content>
  </entry>
</feed>
"""

# The whole-partition read rename and resource changes modify and write back (#1057).
LPAR_ENTRY = (
    '<entry xmlns="http://www.w3.org/2005/Atom"><content>'
    '<LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">'
    "<PartitionName>owned-lpar</PartitionName>"
    + LPAR_RESOURCE_CONFIG
    + "</LogicalPartition></content></entry>"
)


def _mock_partition_read(router) -> None:
    router.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=LPAR_ENTRY, headers={"ETag": "etag-1"})
    )


EMPTY_FEED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom"/>
"""

REPO = {"type": "nfs", "host": "repo.example.com", "path": "/images/hmc"}


def _hmc_env(monkeypatch) -> None:
    """Set env vars so HMCConfig() succeeds inside the tool."""
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")


def _make_ssh_mock(stdout: str = "") -> MagicMock:
    """Return a minimal asyncssh connection mock."""
    result = MagicMock()
    result.stdout = stdout

    conn = AsyncMock()
    conn.run = AsyncMock(return_value=result)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn


# ---------------------------------------------------------------------- #
# hmc_run_command (SSH passthrough)
# ---------------------------------------------------------------------- #


def test_run_command_passes_cmd_through(monkeypatch):
    """hmc_run_command runs the exact command over SSH and returns stdout."""
    _hmc_env(monkeypatch)
    conn_mock = _make_ssh_mock("lpar1  running\n")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_run_command("lssyscfg -r lpar -m server1")

    called_cmd = conn_mock.run.call_args[0][0]
    assert called_cmd == "lssyscfg -r lpar -m server1"
    assert result == "lpar1  running\n"


# ---------------------------------------------------------------------- #
# hmc_get_job / hmc_list_lpars name-lookup (REST reads)
# ---------------------------------------------------------------------- #


def test_get_job_parses_entry(monkeypatch, mock_hmc):
    """hmc_get_job returns the normalized outcome with the raw resource."""
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/jobs/{RUNNING_JOB_ID}").mock(
        return_value=httpx.Response(200, text=RUNNING_JOB_ENTRY)
    )
    result = hmc_get_job(RUNNING_JOB_ID)
    assert result.job["Resource"]["JobID"] == RUNNING_JOB_ID
    assert result.job["Resource"]["Status"] == "RUNNING"


def test_get_job_empty_returns_missing_outcome(monkeypatch, mock_hmc):
    """hmc_get_job reports found false when the server returns no content."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/jobs/job-uuid-999").mock(
        return_value=httpx.Response(204)
    )
    outcome = hmc_get_job("job-uuid-999")
    assert outcome.found is False
    assert outcome.job is None
    assert outcome.job_href is None


def test_get_job_reaped_returns_missing_outcome(monkeypatch, mock_hmc):
    """A job the HMC no longer has reads as no job, not as an HMCError (#474)."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/jobs/job-uuid-999").mock(
        return_value=live_response("rest-job-not-found")[1]
    )
    outcome = hmc_get_job("job-uuid-999")
    assert outcome.found is False
    assert outcome.job is None
    assert outcome.job_href is None


def test_get_job_degraded_hmc_still_raises(monkeypatch, mock_hmc):
    """Only "no job there" reads as no job; a failing HMC still raises (#474)."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/jobs/job-uuid-999").mock(
        return_value=httpx.Response(503, text="service unavailable")
    )
    with pytest.raises(HMCError) as exc_info:
        hmc_get_job("job-uuid-999")
    assert exc_info.value.status_code == 503


def test_get_job_rejects_identifier_addressing_something_else(monkeypatch, mock_hmc):
    """An identifier carrying a path is rejected, not reported as a missing job."""
    _hmc_env(monkeypatch)
    with pytest.raises(ValueError, match="not an HMC job identifier"):
        hmc_get_job("jobs/job-uuid-999")


def test_get_job_requires_identifier_even_with_href(monkeypatch, mock_hmc):
    """The identifier is now checked even when job_href would decide the path.

    The client ignored ``job_id`` entirely when a link was supplied, so this is
    the one rejection that changes behaviour rather than moving an error (#474).
    """
    _hmc_env(monkeypatch)
    with pytest.raises(ValueError, match="must be a non-empty HMC job identifier"):
        hmc_get_job("", job_href=_JOB_OP_HREF)


def test_get_job_trims_surrounding_whitespace(monkeypatch, mock_hmc):
    """Padding from a stored handle is trimmed, not rejected — as documented."""
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, text=JOB_ENTRY)
    )
    assert hmc_get_job(f"  {JOB_ID}  ").job_id == JOB_ID


def test_lpars_by_name(monkeypatch, mock_hmc):
    """hmc_list_lpars resolves a non-UUID selector as a PartitionName."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/LogicalPartition/search/(PartitionName==aixprod)").mock(
        return_value=httpx.Response(200, text=LPAR_FEED.format(name="aixprod"))
    )
    result = hmc_get_lpar("aixprod")
    assert result["UUID"] == LPAR_UUID
    assert result["Resource"]["PartitionName"] == "aixprod"


def test_lpars_name_not_found_returns_none(monkeypatch, mock_hmc):
    """hmc_get_lpar returns None when a name matches nothing and every system operates."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/LogicalPartition/search/(PartitionName==ghost)").mock(
        return_value=httpx.Response(200, text=EMPTY_FEED)
    )
    mock_system_summaries(mock_hmc, (SYSTEM_UUID, "sys-E2", "operating"))
    assert hmc_get_lpar("ghost") is None


# ---------------------------------------------------------------------- #
# hmc_power_on_lpar / hmc_power_off_lpar (job submission)
# ---------------------------------------------------------------------- #


def test_power_on_lpar_submits_job(monkeypatch, mock_hmc):
    """hmc_power_on_lpar PUTs a PowerOn job to the do/ path."""
    _hmc_env(monkeypatch)
    # Mock the precondition state check (not activated → proceed with PowerOn).
    mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/quick/PartitionState"
    ).mock(return_value=httpx.Response(200, text='"not activated"'))
    route = mock_hmc.put(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/do/PowerOn").mock(
        return_value=httpx.Response(202, text=JOB_ENTRY)
    )
    result = hmc_power_on_lpar(LPAR_UUID)
    assert route.called
    body = route.calls.last.request.content.decode()
    assert "PowerOn</OperationName>" in body
    assert result.already_activated is False
    assert result.job["Resource"]["JobID"] == JOB_ID
    assert result.message is None


def test_power_on_lpar_tool_forwards_activation_parameters(monkeypatch, mock_hmc):
    """The three activation parameters reach the PowerOn job document."""
    _hmc_env(monkeypatch)
    mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/quick/PartitionState"
    ).mock(return_value=httpx.Response(200, text='"not activated"'))
    # ADR 0039 containment: the tool reads the partition's own profile feed
    # before it will carry a caller-supplied LogicalPartitionProfile.
    mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/LogicalPartitionProfile"
    ).mock(
        return_value=httpx.Response(
            200, text=PARTITION_PROFILE_FEED.format(uuid=PARTITION_PROFILE_UUID)
        )
    )
    # #981: the profile check reads the current client adapters; none here.
    for adapter_type in (
        "VirtualSCSIClientAdapter",
        "VirtualFibreChannelClientAdapter",
        "ClientNetworkAdapter",
    ):
        response = (
            httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>')
            if adapter_type == "ClientNetworkAdapter"
            else httpx.Response(204)
        )
        mock_hmc.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/{adapter_type}").mock(
            return_value=response
        )
    route = mock_hmc.put(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/do/PowerOn").mock(
        return_value=httpx.Response(202, text=JOB_ENTRY)
    )

    outcome = hmc_power_on_lpar(
        LPAR_UUID,
        boot_mode="sms",
        partition_profile_uuid=PARTITION_PROFILE_UUID,
        operation_type="activate",
        keylock="manual",
    )

    assert outcome.warnings == ()

    body = route.calls.last.request.content.decode()
    assert ">bootmode</ParameterName>" in body
    assert '<ParameterValue kb="CUR" kxe="false">sms</ParameterValue>' in body
    assert ">LogicalPartitionProfile</ParameterName>" in body
    assert PARTITION_PROFILE_UUID in body
    # #1392: activate is accepted and never sent; V10R3 refuses OperationType.
    assert ">OperationType</ParameterName>" not in body
    assert ">keylock</ParameterName>" in body
    assert '<ParameterValue kb="CUR" kxe="false">manual</ParameterValue>' in body

    # ADR 0161: the partition profile is never named `profile`, which is the
    # connection profile and stays exactly what it was.
    parameters = inspect.signature(hmc_power_on_lpar).parameters
    assert {"boot_mode", "partition_profile_uuid", "operation_type", "keylock"} <= set(
        parameters
    )
    assert parameters["profile"].default is None


def test_power_off_lpar_submits_job(monkeypatch, mock_hmc):
    """hmc_power_off_lpar PUTs a PowerOff job with the immediate flag."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/do/PowerOff"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    hmc_power_off_lpar(LPAR_UUID, immediate=True)
    body = route.calls.last.request.content.decode()
    assert "PowerOff</OperationName>" in body
    assert '<ParameterName kb="ROR" kxe="false">immediate</ParameterName>' in body
    assert '<ParameterValue kb="CUR" kxe="false">true</ParameterValue>' in body


def test_power_off_lpar_tool_forwards_shutdown_parameters(monkeypatch, mock_hmc):
    """restart and operation reach the PowerOff job document."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/do/PowerOff"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))

    hmc_power_off_lpar(LPAR_UUID, restart=True, operation="osshutdown")

    body = route.calls.last.request.content.decode()
    assert '<ParameterName kb="ROR" kxe="false">restart</ParameterName>' in body
    assert '<ParameterValue kb="CUR" kxe="false">true</ParameterValue>' in body
    assert '<ParameterName kb="ROR" kxe="false">operation</ParameterName>' in body
    assert '<ParameterValue kb="CUR" kxe="false">osshutdown</ParameterValue>' in body


def test_power_off_lpar_tool_cannot_send_dumprestart(monkeypatch, mock_hmc):
    """ADR 0188: the crash is not this tool's, so a direct call cannot reach it."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/do/PowerOff"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))

    assert "allow_dump_restart" not in inspect.signature(hmc_power_off_lpar).parameters
    with pytest.raises(ValueError, match="allow_dump_restart"):
        hmc_power_off_lpar(LPAR_UUID, operation="dumprestart")
    assert route.called is False


def test_dump_restart_lpar_tool_submits_dumprestart(monkeypatch, mock_hmc):
    """ADR 0188: the crash is its own tool and sends operation=dumprestart."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/do/PowerOff"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))

    hmc_dump_restart_lpar(LPAR_UUID, allow_dump_restart=True)

    body = route.calls.last.request.content.decode()
    assert '<ParameterName kb="ROR" kxe="false">operation</ParameterName>' in body
    assert '<ParameterValue kb="CUR" kxe="false">dumprestart</ParameterValue>' in body
    assert '<ParameterValue kb="CUR" kxe="false">true</ParameterValue>' not in body


def test_dump_restart_lpar_tool_refuses_without_opt_in(monkeypatch, mock_hmc):
    """ADR 0164's confirmation stays on the crash path: no flag, no job."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/do/PowerOff"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))

    with pytest.raises(ValueError, match="allow_dump_restart"):
        hmc_dump_restart_lpar(LPAR_UUID)

    assert route.called is False


# ---------------------------------------------------------------------- #
# hmc_create_lpar / hmc_modify_lpar (argument mapping -> XML)
# ---------------------------------------------------------------------- #


def test_create_lpar_builds_xml(monkeypatch, mock_hmc):
    """hmc_create_lpar maps its arguments into the LogicalPartition document."""
    _hmc_env(monkeypatch)
    # The name-collision check reads the target system: no LPAR with this name.
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(200, text=LPAR_FEED.format(name="other-lpar"))
    )
    route = mock_hmc.put(
        f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition"
    ).mock(return_value=httpx.Response(201, text=LPAR_FEED.format(name="newlpar")))
    # system name resolution for stamp (REST-first: get_managed_system)
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}").mock(
        return_value=httpx.Response(
            200,
            text=f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:{SYSTEM_UUID}</id>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <ManagedSystem xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <SystemName>server1</SystemName>
    </ManagedSystem>
  </content>
</entry>""",
        )
    )
    mock_system_summary(mock_hmc, SYSTEM_UUID, "server1")
    # stamp_lpar_ownership calls set_lpar_description over SSH;
    # patch stamp to avoid needing a live SSH server in this XML-building test.
    with patch(
        "hmcpctl.operations.lpar.ownership.stamp_lpar_ownership",
        new=AsyncMock(return_value="[hmcpctl owner:hmcpctl created:2026-01-01]"),
    ):
        result = hmc_create_lpar(
            system_name_or_uuid=SYSTEM_UUID,
            name="newlpar",
            apply_partition_profile=False,
            resources=LparResources(
                min_memory=512,
                desired_memory=2048,
                max_memory=4096,
                desired_procs=0.5,
                desired_vcpus=2,
            ),
        )
    assert route.called
    body = route.calls.last.request.content.decode()
    assert "newlpar</PartitionName>" in body
    assert '<DesiredMemory kb="CUD" kxe="false">2048</DesiredMemory>' in body
    assert '<MinimumMemory kb="CUD" kxe="false">512</MinimumMemory>' in body
    assert (
        '<DesiredProcessingUnits kb="CUD" kxe="false">0.5</DesiredProcessingUnits>'
        in body
    )
    assert (
        '<DesiredVirtualProcessors kb="CUD" kxe="false">2</DesiredVirtualProcessors>'
        in body
    )
    # result is now wrapped: {"lpar": <entry>, "ownership_stamped": ..., "warnings": []}
    assert result.lpar["Resource"]["PartitionName"] == "newlpar"
    assert result.ownership_stamped is True
    assert result.warnings == ()


def test_create_lpar_dedicated_uses_whole_cpus(monkeypatch, mock_hmc):
    """dedicated=True emits DedicatedProcessorConfiguration with int CPUs."""
    _hmc_env(monkeypatch)
    # The name-collision check reads the target system: no LPAR with this name.
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(200, text=LPAR_FEED.format(name="other-lpar"))
    )
    route = mock_hmc.put(
        f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition"
    ).mock(return_value=httpx.Response(201, text=LPAR_FEED.format(name="ded")))
    with (
        patch(
            "hmcpctl.operations.lpar.ownership.stamp_lpar_ownership",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "hmcpctl.operations.lpar.ownership._resolve_system_name",
            new=AsyncMock(return_value="sys1"),
        ),
    ):
        hmc_create_lpar(
            system_name_or_uuid=SYSTEM_UUID,
            name="ded",
            resources=LparResources(dedicated=True, desired_procs=2.0, max_procs=4.0),
        )
    body = route.calls.last.request.content.decode()
    assert "<DedicatedProcessorConfiguration" in body
    assert '<DesiredProcessors kb="CUD" kxe="false">2</DesiredProcessors>' in body
    assert (
        '<HasDedicatedProcessors kb="CUD" kxe="false">true</HasDedicatedProcessors>'
        in body
    )


def test_modify_lpar_builds_resource_xml(monkeypatch, mock_hmc):
    """hmc_modify_lpar changes only the fields passed; the rest are written back as read."""
    _hmc_env(monkeypatch)
    _mock_partition_read(mock_hmc)
    route = mock_hmc.post(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=LPAR_FEED.format(name="owned-lpar"))
    )
    with patch(
        "hmcpctl.operations.lpar.dlpar.resolve_and_authorize_lpar_mutation",
        new=AsyncMock(return_value=LPAR_UUID),
    ):
        result = hmc_modify_lpar(
            LPAR_UUID,
            resources=LparResources(desired_memory=8192),
        )
    body = route.calls.last.request.content.decode()
    assert "<DesiredMemory>8192</DesiredMemory>" in body
    assert "<PartitionName>owned-lpar</PartitionName>" in body
    assert "<MaximumMemory>16384</MaximumMemory>" in body
    assert "<DesiredProcessingUnits>0.5</DesiredProcessingUnits>" in body
    assert result["Resource"]["PartitionName"] == "owned-lpar"


def test_rename_lpar_authorizes_and_writes_name(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    _mock_partition_read(mock_hmc)
    route = mock_hmc.post(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=LPAR_FEED.format(name="renamed"))
    )
    guard = AsyncMock(return_value=LPAR_UUID)
    with patch(
        "hmcpctl.operations.lpar.core.resolve_and_authorize_lpar_mutation",
        new=guard,
    ):
        result = hmc_rename_lpar(
            SYSTEM_UUID,
            LPAR_UUID,
            "renamed",
            ownership_override=True,
        )

    body = route.calls.last.request.content.decode()
    assert "<PartitionName>renamed</PartitionName>" in body
    guard.assert_awaited_once_with(
        ANY, LPAR_UUID, system_name_or_uuid=SYSTEM_UUID, ownership_override=True
    )
    assert result["Resource"]["PartitionName"] == "renamed"


def test_foreign_owned_rename_issues_no_write(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=LPAR_FEED.format(name="owned-lpar"))
    )
    write = mock_hmc.post(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}")
    with (
        patch(
            "hmcpctl.operations.lpar.core.resolve_and_authorize_lpar_mutation",
            new=AsyncMock(side_effect=PermissionError("foreign owner")),
        ),
        pytest.raises(PermissionError, match="foreign owner"),
    ):
        hmc_rename_lpar(SYSTEM_UUID, LPAR_UUID, "renamed")
    assert not write.called


def test_foreign_owned_delete_issues_no_write(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=LPAR_FEED.format(name="owned-lpar"))
    )
    write = mock_hmc.delete(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}")
    with (
        patch(
            "hmcpctl.operations.lpar.core.resolve_and_authorize_lpar_mutation",
            new=AsyncMock(side_effect=PermissionError("foreign owner")),
        ),
        pytest.raises(PermissionError, match="foreign owner"),
    ):
        hmc_delete_lpar(SYSTEM_UUID, LPAR_UUID)
    assert not write.called


# ---------------------------------------------------------------------- #
# Update / upgrade tools (job submission)
# ---------------------------------------------------------------------- #


def test_hmc_update_kind_update(monkeypatch, mock_hmc):
    """Console updates use IBM's documented operation and parameters."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(
        f"/rest/api/uom/ManagementConsole/{MC_UUID}/do/UpdateManagementConsole"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    hmc_update_console_software(MC_UUID, CONSOLE_SOURCE)
    body = route.calls.last.request.content.decode()
    assert "UpdateManagementConsole</OperationName>" in body
    assert "MediaType</ParameterName>" in body
    assert "ServerHostOrIP</ParameterName>" in body
    assert "repo.example.com" in body
    assert "/images/hmc" in body


def test_hmc_update_submits_documented_operation(monkeypatch, mock_hmc):
    """hmc_update_console_software has one supported update operation."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(
        f"/rest/api/uom/ManagementConsole/{MC_UUID}/do/UpdateManagementConsole"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    hmc_update_console_software(MC_UUID, CONSOLE_SOURCE)
    assert route.calls.last.request.url.path.endswith("/do/UpdateManagementConsole")


def test_hmc_update_encodes_console_uuid_as_one_path_segment(monkeypatch, mock_hmc):
    """A selector cannot redirect the authorized update to another operation."""
    _hmc_env(monkeypatch)
    hostile_uuid = "allowed/do/ShutdownHMC?ignored="
    route = mock_hmc.put(
        "/rest/api/uom/ManagementConsole/allowed%2Fdo%2FShutdownHMC%3Fignored%3D"
        "/do/UpdateManagementConsole"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))

    hmc_update_console_software(hostile_uuid, CONSOLE_SOURCE)

    assert route.called


def test_vios_update_submits_documented_job(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    route = mock_hmc.put(
        f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/do/UpdateVIOS"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    hmc_vios_update(VIOS_UUID, VIOS_UPDATE_SOURCE)
    body = route.calls.last.request.content.decode()
    assert "UpdateVIOS</OperationName>" in body
    assert "repo.example.com" in body


def test_vios_upgrade_submits_documented_job(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    route = mock_hmc.put(
        f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/do/UpgradeVIOS"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    hmc_vios_upgrade(VIOS_UUID, VIOS_UPGRADE_SOURCE)
    body = route.calls.last.request.content.decode()
    assert "UpgradeVIOS</OperationName>" in body
    assert "repo.example.com" in body


def test_vios_update_encodes_uuid_as_one_path_segment(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    hostile_uuid = "allowed/do/Shutdown?ignored="
    monkeypatch.setattr(
        "hmcpctl.operations.updates.service.resolve_vios_uuid",
        AsyncMock(return_value=hostile_uuid),
    )
    route = mock_hmc.put(
        "/rest/api/uom/VirtualIOServer/allowed%2Fdo%2FShutdown%3Fignored%3D"
        "/do/UpdateVIOS"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))

    hmc_vios_update(hostile_uuid, VIOS_UPDATE_SOURCE)

    assert route.called


@pytest.mark.parametrize(
    ("tool", "source", "message"),
    [
        (hmc_vios_update, {"Name": "image"}, "ResourceType"),
        (hmc_vios_update, {"ResourceType": "NFS", "unknown": "x"}, "unknown"),
        (hmc_vios_update, {"ResourceType": "NFS", "Disks": "hdisk1"}, "Disks"),
        (
            hmc_vios_upgrade,
            {"ResourceType": "NFS", "RestartVIOS": "false"},
            "RestartVIOS",
        ),
        (hmc_vios_upgrade, {"ResourceType": "IBMWebsite"}, "IBMWebsite"),
        (hmc_vios_update, {"ResourceType": "NFS"}, "RemoteDirectory"),
        (hmc_vios_upgrade, {"ResourceType": "HMC", "Name": "image"}, "Disks"),
    ],
)
def test_vios_invalid_source_fails_before_submission(
    monkeypatch, mock_hmc, tool, source, message
):
    _hmc_env(monkeypatch)
    route = mock_hmc.put(url__regex=r".*/VirtualIOServer/.*/do/.*")

    with pytest.raises(ValueError, match=message):
        tool(VIOS_UUID, source)
    assert not route.called


def _vios_job_with_stdout(status="COMPLETED_OK", top_level=None):
    job = {
        "Resource": {
            "Status": status,
            "Results": {
                "JobParameter": {
                    "ParameterName": "stdOut",
                    "ParameterValue": "  install log  ",
                }
            },
        }
    }
    if top_level is not None:
        job["stdOut"] = top_level
    return job


def _mock_vios_submission(mock_hmc):
    return mock_hmc.put(
        f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/do/UpdateVIOS"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))


def test_vios_waited_terminal_result_projects_stdout(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    _mock_vios_submission(mock_hmc)
    raw = _vios_job_with_stdout()
    monkeypatch.setattr(
        "hmcpctl.operations.updates.service.wait_for_submitted_job",
        AsyncMock(return_value=raw),
    )

    result = hmc_vios_update(VIOS_UUID, VIOS_UPDATE_SOURCE, wait=True)

    assert result["stdOut"] == "install log"
    assert result is not raw
    assert "stdOut" not in raw
    assert result["Resource"] is raw["Resource"]


@pytest.mark.parametrize(
    ("wait", "job"),
    [
        (False, _vios_job_with_stdout()),
        (True, _vios_job_with_stdout(status="RUNNING")),
    ],
)
def test_vios_stdout_is_not_projected_without_terminal_wait(
    monkeypatch, mock_hmc, wait, job
):
    _hmc_env(monkeypatch)
    _mock_vios_submission(mock_hmc)
    monkeypatch.setattr(
        "hmcpctl.operations.updates.service.wait_for_submitted_job",
        AsyncMock(return_value=job),
    )

    result = hmc_vios_update(VIOS_UUID, VIOS_UPDATE_SOURCE, wait=wait)

    assert result is job
    assert "stdOut" not in result


def test_vios_stdout_does_not_overwrite_raw_top_level_value(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    _mock_vios_submission(mock_hmc)
    raw = _vios_job_with_stdout(top_level="raw value")
    monkeypatch.setattr(
        "hmcpctl.operations.updates.service.wait_for_submitted_job",
        AsyncMock(return_value=raw),
    )

    result = hmc_vios_update(VIOS_UUID, VIOS_UPDATE_SOURCE, wait=True)

    assert result is raw
    assert result["stdOut"] == "raw value"


# PlatformUpdate answers in JSON (docs/refs/hmc-rest-api-p11/jobs/managedsystem-jobs/
# 065-platformupdate_managedsystem-job.md:303-343), which no capture holds, so its
# replies are documented-shape dicts carrying captured statuses: COMPLETED_OK and
# RUNNING, never a bare COMPLETED (#1202). A poll answers with the captured job read.
def test_update_firmware_submits_platform_update(monkeypatch, mock_hmc):
    """A supported HMC receives the documented native JSON PlatformUpdate."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/ManagementConsole").mock(
        return_value=httpx.Response(200, text=_console_feed(("11", "1", "1111")))
    )
    route = mock_hmc.put(
        f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/do/PlatformUpdate"
    ).mock(
        return_value=httpx.Response(
            202,
            json={
                "id": "platform-job",
                "content": {"JobResponse": {"Status": "COMPLETED_OK"}},
                "selfLink": None,
            },
        )
    )

    result = hmc_update_firmware(SYSTEM_UUID, PLATFORM_UPDATE)

    assert route.called
    assert result == {
        "UUID": "platform-job",
        "Resource": {"Status": "COMPLETED_OK"},
    }
    assert json.loads(route.calls.last.request.content) == {
        "JobRequest": {
            "RequestedOperation": {
                "OperationName": "PlatformUpdate",
                "GroupName": "ManagedSystem",
            },
            "JobParameters": {
                "JobParameter": [
                    {
                        "ParameterName": "PlatformUpdateParameter",
                        "ParameterValue": {
                            "SystemFirmwareUpdate": {
                                "UpdateType": "Update",
                                "UpdateOrder": 1,
                            }
                        },
                    }
                ]
            },
        }
    }


@pytest.mark.parametrize(
    "version",
    [
        ("10", "3", "1060"),
        ("11", "1", "1110"),
        ("secret\nvalue", "1", "1"),
        ("9" * 5000, "1", "1"),
    ],
)
def test_update_firmware_rejects_unsupported_hmc_version(
    monkeypatch, mock_hmc, version
):
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/ManagementConsole").mock(
        return_value=httpx.Response(200, text=_console_feed(version))
    )
    route = mock_hmc.put(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/do/PlatformUpdate")

    with pytest.raises(
        ValueError, match="PlatformUpdate requires HMC 11.1.1111"
    ) as error:
        hmc_update_firmware(SYSTEM_UUID, PLATFORM_UPDATE)

    assert "secret" not in str(error.value)
    assert "9" * 50 not in str(error.value)
    assert not route.called


def test_update_firmware_rejects_missing_hmc_version(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/ManagementConsole").mock(
        return_value=httpx.Response(200, text=_console_feed(None))
    )
    route = mock_hmc.put(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/do/PlatformUpdate")

    with pytest.raises(ValueError, match="PlatformUpdate requires HMC 11.1.1111"):
        hmc_update_firmware(SYSTEM_UUID, PLATFORM_UPDATE)

    assert not route.called


def test_update_firmware_refuses_a_named_system_before_resolving_it(
    monkeypatch, mock_hmc
):
    """The live V10R3 check passes a name; the gate must refuse before any lookup."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/ManagementConsole").mock(
        return_value=httpx.Response(200, text=_console_feed(("10", "3", "1060")))
    )
    lookup = mock_hmc.route(url__regex=r".*/ManagedSystem.*")

    with pytest.raises(ValueError, match="HMC 11.1.1111 .* below the minimum"):
        hmc_update_firmware("sys-A", PLATFORM_UPDATE)

    assert not lookup.called


def test_update_firmware_wait_returns_terminal_submission_without_poll(
    monkeypatch, mock_hmc
):
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/ManagementConsole").mock(
        return_value=httpx.Response(200, text=_console_feed(("11", "2", "1200")))
    )
    mock_hmc.put(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/do/PlatformUpdate").mock(
        return_value=httpx.Response(
            202,
            json={
                "id": "platform-job",
                "content": {"JobResponse": {"Status": "COMPLETED_OK"}},
                "selfLink": None,
            },
        )
    )
    poll = mock_hmc.get("/rest/api/uom/jobs/platform-job")

    result = hmc_update_firmware(SYSTEM_UUID, PLATFORM_UPDATE, wait=True)

    assert result is not None
    assert result["Resource"]["Status"] == "COMPLETED_OK"
    assert not poll.called


def test_update_firmware_wait_rejects_unpollable_accepted_job(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/ManagementConsole").mock(
        return_value=httpx.Response(200, text=_console_feed(("11", "1", "1111")))
    )
    mock_hmc.put(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/do/PlatformUpdate").mock(
        return_value=httpx.Response(
            202,
            json={
                "id": "platform-job",
                "content": {"JobResponse": {"Status": "RUNNING"}},
                "selfLink": None,
            },
        )
    )

    with pytest.raises(HMCError, match="accepted.*cannot be polled"):
        hmc_update_firmware(SYSTEM_UUID, PLATFORM_UPDATE, wait=True)


def test_update_firmware_wait_polls_supplied_self_link(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/ManagementConsole").mock(
        return_value=httpx.Response(200, text=_console_feed(("11", "1", "1111")))
    )
    mock_hmc.put(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/do/PlatformUpdate").mock(
        return_value=httpx.Response(
            202,
            json={
                "id": "platform-job",
                "content": {"JobResponse": {"Status": "RUNNING"}},
                "selfLink": "/rest/api/uom/jobs/platform-job",
            },
        )
    )
    poll = mock_hmc.get("/rest/api/uom/jobs/platform-job").mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )

    result = hmc_update_firmware(
        SYSTEM_UUID,
        PLATFORM_UPDATE,
        wait=True,
        timeout_seconds=60,
        poll_interval=1,
    )

    assert poll.called
    assert result is not None
    assert result["Resource"]["Status"] == "COMPLETED_OK"


def test_hmc_update_wait_true_polls_to_completion(monkeypatch, mock_hmc):
    """hmc_update_console_software(wait=True) submits the job then polls until COMPLETED_OK."""
    _hmc_env(monkeypatch)
    submit_route = mock_hmc.put(
        f"/rest/api/uom/ManagementConsole/{MC_UUID}/do/UpdateManagementConsole"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    poll_route = mock_hmc.get(f"/rest/api/uom/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )
    result = hmc_update_console_software(
        MC_UUID, CONSOLE_SOURCE, wait=True, timeout_seconds=60, poll_interval=1
    )
    assert submit_route.called
    assert poll_route.called
    assert result["Resource"]["Status"] == "COMPLETED_OK"


def test_submit_available_hmc_ptfs_query_returns_submitted_job(monkeypatch, mock_hmc):
    """The PTF query tool submits the documented list job."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(
        f"/rest/api/uom/ManagementConsole/{MC_UUID}/do/ListManagementConsoleUpdates"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    result = hmc_submit_available_hmc_ptfs_query(MC_UUID)
    assert route.called
    assert result["Resource"]["JobID"] == JOB_ID
    body = route.calls.last.request.content.decode()
    assert "ListManagementConsoleUpdates" in body
    assert "ManagementConsole" in body
    assert "<JobParameter schemaVersion" not in body


def test_submit_available_hmc_ptfs_query_preserves_positional_profile(
    monkeypatch, mock_hmc
):
    _hmc_env(monkeypatch)
    route = mock_hmc.put(
        f"/rest/api/uom/ManagementConsole/{MC_UUID}/do/ListManagementConsoleUpdates"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))

    result = hmc_submit_available_hmc_ptfs_query(MC_UUID, "default")

    assert route.called
    assert result["Resource"]["JobID"] == JOB_ID
    assert all(call.request.method != "GET" for call in mock_hmc.calls)


def test_submit_available_hmc_ptfs_query_surfaces_job_error(monkeypatch, mock_hmc):
    """Job submission errors retain their server diagnosis."""
    _hmc_env(monkeypatch)
    mock_hmc.put(
        f"/rest/api/uom/ManagementConsole/{MC_UUID}/do/ListManagementConsoleUpdates"
    ).mock(return_value=httpx.Response(500, text="repository unavailable"))
    with pytest.raises(HMCError, match="repository unavailable"):
        hmc_submit_available_hmc_ptfs_query(MC_UUID)


def test_submit_available_hmc_ptfs_query_waits_for_result(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    mock_hmc.put(
        f"/rest/api/uom/ManagementConsole/{MC_UUID}/do/ListManagementConsoleUpdates"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    poll = mock_hmc.get(f"/rest/api/uom/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )

    result = hmc_submit_available_hmc_ptfs_query(
        MC_UUID, wait=True, timeout_seconds=60, poll_interval=1
    )

    assert poll.called
    assert result["Resource"]["Status"] == "COMPLETED_OK"


@pytest.mark.parametrize(
    ("timeout_seconds", "poll_interval", "message"),
    [(-1, 1, "timeout_seconds"), (60, 0, "poll_interval")],
)
def test_submit_available_hmc_ptfs_query_validates_wait_timing_before_io(
    monkeypatch, mock_hmc, timeout_seconds, poll_interval, message
):
    _hmc_env(monkeypatch)
    route = mock_hmc.put(
        f"/rest/api/uom/ManagementConsole/{MC_UUID}/do/ListManagementConsoleUpdates"
    )

    with pytest.raises(ValueError, match=message):
        hmc_submit_available_hmc_ptfs_query(
            MC_UUID,
            wait=True,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        )

    assert not route.called


# hmc_wait_for_job
# ---------------------------------------------------------------------- #

# The captured V10R3 COMPLETED_WITH_ERROR PowerOn read (#1161). The ErrorData
# variant renames its `result` parameter to the documented ErrorData name.
JOB_FAILED_PATH, _ = live_response("rest-job-completed-with-error")
JOB_FAILED_ENTRY = live_fixture("rest-job-completed-with-error")["body"]
JOB_RESPONSE_ERROR_DATA = JOB_FAILED_ENTRY.replace(
    ">result</ParameterName>", ">ErrorData</ParameterName>"
)
assert JOB_RESPONSE_ERROR_DATA != JOB_FAILED_ENTRY

JOB_ENTRY_EMPTY_RESOURCE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:job-uuid-999</id>
  <title>Job</title>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <Job xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"/>
  </content>
</entry>
"""

JOB_OUTCOME_KEYS = {
    "job_id",
    "status",
    "timed_out",
    "error",
    "job",
    "found",
    "job_href",
}


def test_wait_for_job_immediate_completed(monkeypatch, mock_hmc):
    """hmc_wait_for_job returns immediately when the first poll is COMPLETED_OK."""
    _hmc_env(monkeypatch)
    route = mock_hmc.get(f"/rest/api/uom/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )
    result = hmc_wait_for_job(JOB_ID)
    assert route.called
    assert set(asdict(result)) == JOB_OUTCOME_KEYS
    assert result.job_id == JOB_ID
    assert result.status == "COMPLETED_OK"
    assert result.timed_out is False
    assert result.error is None
    assert result.job["Resource"]["Status"] == "COMPLETED_OK"


@pytest.mark.parametrize("response", [JOB_FAILED_ENTRY, JOB_RESPONSE_ERROR_DATA])
def test_wait_for_job_surfaces_terminal_failure(monkeypatch, mock_hmc, response):
    _hmc_env(monkeypatch)
    monkeypatch.setenv("HMC_VERIFY_SSL", "true")
    mock_hmc.get(JOB_FAILED_PATH).mock(return_value=httpx.Response(200, text=response))
    job_id = JOB_FAILED_PATH.rsplit("/", 1)[-1]

    result = hmc_wait_for_job(job_id)

    assert set(asdict(result)) == JOB_OUTCOME_KEYS
    assert result.job_id == job_id
    assert result.status == "COMPLETED_WITH_ERROR"
    assert result.timed_out is False
    assert result.error == (
        "HSCL3681 Partition 1 cannot be activated since it is not in the "
        "Not Activated state."
    )
    assert result.job["Resource"]["Status"] == "COMPLETED_WITH_ERROR"


def test_wait_for_job_timeout_is_explicit(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/jobs/{RUNNING_JOB_ID}").mock(
        return_value=httpx.Response(200, text=RUNNING_JOB_ENTRY)
    )
    # timeout=0 means the deadline is already past after the first poll
    result = hmc_wait_for_job(RUNNING_JOB_ID, timeout_seconds=0, poll_interval=1)
    assert set(asdict(result)) == JOB_OUTCOME_KEYS
    assert result.job_id == RUNNING_JOB_ID
    assert result.status == "RUNNING"
    assert result.timed_out is True
    assert result.error is None
    assert result.job["Resource"]["Status"] == "RUNNING"


def test_wait_for_job_empty_resource_returns_timed_out_shape(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    monkeypatch.setenv("HMC_VERIFY_SSL", "true")
    mock_hmc.get("/rest/api/uom/jobs/job-uuid-999").mock(
        return_value=httpx.Response(200, text=JOB_ENTRY_EMPTY_RESOURCE)
    )

    result = hmc_wait_for_job("job-uuid-999", timeout_seconds=0, poll_interval=1)

    assert set(asdict(result)) == JOB_OUTCOME_KEYS
    assert result.job_id == "job-uuid-999"
    assert result.status is None
    assert result.timed_out is True
    assert result.error is None
    assert result.job["Resource"] == ""


def test_wait_for_job_reaped_returns_found_false(monkeypatch, mock_hmc):
    """A reaped job comes back as found=False, not as an HMCError (#474)."""
    _hmc_env(monkeypatch)
    route = mock_hmc.get("/rest/api/uom/jobs/job-uuid-999").mock(
        return_value=live_response("rest-job-not-found")[1]
    )

    result = hmc_wait_for_job("job-uuid-999", timeout_seconds=300, poll_interval=5)

    assert set(asdict(result)) == JOB_OUTCOME_KEYS
    assert result.found is False
    assert result.job_id == "job-uuid-999"
    assert result.status is None
    assert result.error is None
    assert result.job is None
    assert result.job_href is None
    # timed_out rides along on a gone job because no terminal status was seen;
    # it is not a "still running" signal, which is why found is read first.
    assert result.timed_out is True
    # The whole point: gone is answered on the first poll, not after the timeout.
    assert route.call_count == 1


def test_wait_for_job_empty_feed_is_also_reported_gone(monkeypatch, mock_hmc):
    """The other way the HMC produces no entry ends the wait the same way (#474)."""
    _hmc_env(monkeypatch)
    route = mock_hmc.get("/rest/api/uom/jobs/job-uuid-999").mock(
        return_value=httpx.Response(204)
    )

    result = hmc_wait_for_job("job-uuid-999", timeout_seconds=300, poll_interval=5)

    assert result.found is False
    assert result.status is None
    assert route.call_count == 1


def test_wait_for_job_running_is_found_true(monkeypatch, mock_hmc):
    """The other side of the distinction: still running is found=True (#474)."""
    _hmc_env(monkeypatch)
    mock_hmc.get(f"/rest/api/uom/jobs/{RUNNING_JOB_ID}").mock(
        return_value=httpx.Response(200, text=RUNNING_JOB_ENTRY)
    )

    result = hmc_wait_for_job(RUNNING_JOB_ID, timeout_seconds=0, poll_interval=1)

    assert result.found is True
    assert result.timed_out is True
    assert result.status == "RUNNING"


def test_wait_for_job_degraded_hmc_still_raises(monkeypatch, mock_hmc):
    """A failing HMC is not a vanished job: it still aborts the wait (#474)."""
    _hmc_env(monkeypatch)
    mock_hmc.get("/rest/api/uom/jobs/job-uuid-999").mock(
        return_value=httpx.Response(503, text="service unavailable")
    )
    with pytest.raises(HMCError) as exc_info:
        hmc_wait_for_job("job-uuid-999", timeout_seconds=0, poll_interval=1)
    assert exc_info.value.status_code == 503


def test_wait_for_job_rejects_identifier_addressing_something_else(
    monkeypatch, mock_hmc
):
    """An identifier carrying a path is rejected, not reported as found=False."""
    _hmc_env(monkeypatch)
    with pytest.raises(ValueError, match="not an HMC job identifier"):
        hmc_wait_for_job("jobs/job-uuid-999")


# ---------------------------------------------------------------------- #
# hmc_get_job / hmc_wait_for_job — SELF-link-based polling (issue #95)
# ---------------------------------------------------------------------- #

# The captured submission's SELF link path. Paired with an entry-UUID job_id, it
# is the handle an earlier release stored; per-operation links are refused (#1202).
_JOB_OP_HREF = f"/rest/api/uom/jobs/{JOB_ID}"


def test_get_job_with_href_uses_direct_path(monkeypatch, mock_hmc):
    """hmc_get_job(entry_uuid, job_href=...) reads the link, not jobs/{entry_uuid}."""
    _hmc_env(monkeypatch)
    href_route = mock_hmc.get(_JOB_OP_HREF).mock(
        return_value=httpx.Response(200, text=JOB_ENTRY)
    )
    entry_uuid_path, refused = live_response("rest-job-entry-uuid-refused")
    global_route = mock_hmc.get(entry_uuid_path).mock(return_value=refused)
    result = hmc_get_job(entry_uuid_path.rsplit("/", 1)[-1], job_href=_JOB_OP_HREF)
    assert href_route.called
    assert not global_route.called
    assert result.job["Resource"]["JobID"] == JOB_ID


@pytest.mark.parametrize("tool", [hmc_get_job, hmc_wait_for_job])
@pytest.mark.parametrize("control", ["\t", "\r", "\n"])
def test_job_tools_reject_parser_deleted_job_href_controls(
    monkeypatch, mock_hmc, caplog, tool, control
):
    """Control-bearing caller input is rejected without a request or log leak."""
    _hmc_env(monkeypatch)
    payload = '{"level":"error","message":"forged"}'
    forged = f"{_JOB_OP_HREF}{control}{payload}"

    with (
        caplog.at_level(logging.WARNING, logger="hmcpctl.operations.jobs"),
        pytest.raises(
            ValueError, match="job_href must not contain TAB, CR, or LF"
        ) as exc_info,
    ):
        tool("job-uuid-999", job_href=forged)

    assert forged not in str(exc_info.value)
    assert not caplog.records
    assert not [
        call
        for call in mock_hmc.calls
        if call.request.url.path.startswith("/rest/api/uom/")
    ]


def test_wait_for_job_with_href_uses_direct_path(monkeypatch, mock_hmc):
    """hmc_wait_for_job(uuid, ..., job_href=...) polls the exact href path."""
    _hmc_env(monkeypatch)
    href_route = mock_hmc.get(_JOB_OP_HREF).mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )
    global_route = mock_hmc.get("/rest/api/uom/jobs/job-uuid-999").mock(
        return_value=live_response("rest-job-entry-uuid-refused")[1]
    )
    result = hmc_wait_for_job(
        "job-uuid-999", timeout_seconds=5, poll_interval=1, job_href=_JOB_OP_HREF
    )
    assert href_route.called
    assert not global_route.called
    assert result.status == "COMPLETED_OK"
    assert result.timed_out is False
    # A supplied link that resolved is echoed back, which is what makes a null
    # job_href on a found outcome mean "your link was retired" (#474).
    assert result.job_href == _JOB_OP_HREF


def test_power_on_with_wait_polls_the_submission_jobid(monkeypatch, mock_hmc):
    """hmc_power_on_lpar(wait=True) polls the captured submission's JobID link.

    The submission's entry UUID differs from its JobID, and V10R3 refuses the
    entry UUID on jobs/{id} (#1160), so that route must stay untouched.
    """
    _hmc_env(monkeypatch)
    mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/quick/PartitionState"
    ).mock(
        side_effect=[
            httpx.Response(200, text='"not activated"'),
            httpx.Response(200, text='"running"'),
        ]
    )
    mock_hmc.put(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/do/PowerOn").mock(
        return_value=httpx.Response(200, text=JOB_ENTRY)
    )
    poll_route = mock_hmc.get(f"/rest/api/uom/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )
    entry_uuid_path, refused = live_response("rest-job-entry-uuid-refused")
    entry_uuid_route = mock_hmc.get(entry_uuid_path).mock(return_value=refused)
    result = hmc_power_on_lpar(LPAR_UUID, wait=True, poll_interval=1)
    assert poll_route.called
    assert not entry_uuid_route.called
    assert result.already_activated is False
    assert result.job["Resource"]["Status"] == "COMPLETED_OK"
    assert result.message is None
