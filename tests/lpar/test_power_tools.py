"""Tool-layer tests for the DLPAR and system/VIOS power MCP tools.

The read-modify-write and client methods are covered in test_lpar_rmw.py and
test_power.py; these tests call the actual ``@mcp.tool`` functions in
``server_tools.lpar.lifecycle`` against the respx ``mock_hmc`` router so the
argument->URL and argument->XML mapping in the tool bodies is exercised.
LPAR create/modify and LPAR power-on/off are covered in
``tests/app/test_server_tools.py``; the delete-LPAR precondition guard is
covered in ``tests/app/test_capabilities.py``.
"""

import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from conftest import (
    JOB_ENTRY,
    JOB_ID,
    LPAR_RESOURCE_CONFIG,
    SYSTEM_ENTRY,
    mock_system_summary,
)

from hmcpctl.documents.lpar import LparResources
from hmcpctl.errors import HMCError
from hmcpctl.operations.systems.core import power_system
from hmcpctl.server_tools.lpar.lifecycle import (
    hmc_dlpar_mem,
    hmc_dlpar_proc,
)
from hmcpctl.server_tools.systems.core import (
    hmc_modify_system,
    hmc_power_off_system,
    hmc_power_on_system,
)
from hmcpctl.server_tools.vios.core import (
    hmc_power_off_vios,
    hmc_power_on_vios,
)


@pytest.mark.asyncio
async def test_power_system_operation_rejects_invalid_wait_timing():
    hmc = AsyncMock()
    with pytest.raises(ValueError, match="timeout_seconds"):
        await power_system(
            hmc, SYSTEM_UUID, power_on=False, wait=True, timeout_seconds=-1
        )
    hmc.power_off_system.assert_not_awaited()


SYSTEM_UUID = "00000000-0000-0000-0000-000000000001"
VIOS_UUID = "00000000-0000-0000-0000-000000000003"
LPAR_UUID = "00000000-0000-0000-0000-000000000002"

LPAR_ENTRY = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:{LPAR_UUID}</id>
  <title>LogicalPartition</title>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <PartitionName>lpar1</PartitionName>
{LPAR_RESOURCE_CONFIG}\
      <PartitionState>running</PartitionState>
    </LogicalPartition>
  </content>
</entry>
"""


def _hmc_env(monkeypatch) -> None:
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")


def _mock_dlpar_authorization(router) -> None:
    """The system/partition reads ADR 0092's guard makes before a DLPAR write."""
    router.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}").mock(
        return_value=httpx.Response(
            200, text=SYSTEM_ENTRY.format(uuid=SYSTEM_UUID, name="sys1")
        )
    )
    mock_system_summary(router, SYSTEM_UUID, "sys1")
    router.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=LPAR_ENTRY, headers={"ETag": "etag-1"})
    )
    router.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(200, text=_partition_feed())
    )


def _partition_feed() -> str:
    """The per-system feed both ADR 0094 containment paths read."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        + LPAR_ENTRY.split("?>", 1)[1]
        .strip()
        .replace(' xmlns="http://www.w3.org/2005/Atom"', "", 1)
        + "</feed>"
    )


def _unowned_partition():
    """Patch the SSH ownership read to report a partition with no ADR 0011 stamp."""
    return patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=AsyncMock(return_value=""),
    )


def _mock_owning_system_discovery(router) -> None:
    """The fleet reads ADR 0094's walk makes when the tool omits the selector."""
    feed = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        + SYSTEM_ENTRY.format(uuid=SYSTEM_UUID, name="sys1")
        .split("?>", 1)[1]
        .strip()
        .replace(' xmlns="http://www.w3.org/2005/Atom"', "", 1)
        + "</feed>"
    )
    router.get("/rest/api/uom/ManagedSystem").mock(
        return_value=httpx.Response(200, text=feed)
    )
    router.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(200, text=_partition_feed())
    )


# ---------------------------------------------------------------------- #
# DLPAR
# ---------------------------------------------------------------------- #


def test_dlpar_proc_posts_proc_document(monkeypatch, mock_hmc):
    """hmc_dlpar_proc writes the partition back with only its processor fields changed."""
    _hmc_env(monkeypatch)
    _mock_dlpar_authorization(mock_hmc)
    route = mock_hmc.post(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=LPAR_ENTRY)
    )
    with _unowned_partition():
        result = hmc_dlpar_proc(
            LPAR_UUID,
            LparResources(desired_procs=1.5, desired_vcpus=3),
            system_name_or_uuid=SYSTEM_UUID,
        )
    body = route.calls.last.request.content.decode()
    assert "<DesiredProcessingUnits>1.5<" in body
    assert "<DesiredVirtualProcessors>3<" in body
    assert "<DesiredMemory>4096<" in body
    assert result["Resource"]["PartitionState"] == "running"


def test_dlpar_mem_posts_mem_document(monkeypatch, mock_hmc):
    """hmc_dlpar_mem writes the partition back with only its memory fields changed."""
    _hmc_env(monkeypatch)
    _mock_dlpar_authorization(mock_hmc)
    route = mock_hmc.post(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=LPAR_ENTRY)
    )
    with _unowned_partition():
        result = hmc_dlpar_mem(
            LPAR_UUID,
            LparResources(desired_memory=8192, min_memory=1024, max_memory=16384),
            system_name_or_uuid=SYSTEM_UUID,
        )
    body = route.calls.last.request.content.decode()
    assert "<DesiredMemory>8192<" in body
    assert "<MinimumMemory>1024<" in body
    assert "<MaximumMemory>16384<" in body
    assert "<DesiredProcessingUnits>0.5<" in body
    assert result["Resource"]["PartitionName"] == "lpar1"


def test_dlpar_proc_error_propagates(monkeypatch, mock_hmc):
    """A non-2xx DLPAR POST surfaces as HMCError."""
    _hmc_env(monkeypatch)
    _mock_dlpar_authorization(mock_hmc)
    mock_hmc.post(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(500)
    )
    with _unowned_partition(), pytest.raises(HMCError) as exc_info:
        hmc_dlpar_proc(
            LPAR_UUID,
            LparResources(desired_procs=1.0),
            system_name_or_uuid=SYSTEM_UUID,
        )
    assert exc_info.value.status_code == 500


@pytest.mark.parametrize("tool", [hmc_dlpar_proc, hmc_dlpar_mem])
def test_dlpar_without_a_system_selector_discovers_the_owner_and_writes(
    monkeypatch, mock_hmc, tool
):
    """The default MCP shape — a partition and nothing else — still works.

    This is the invocation every DLPAR client makes today, and the one this
    change reshaped from a single POST into a fleet walk plus an ownership
    read, so it stays covered at the tool boundary rather than only at the
    operation.
    """
    _hmc_env(monkeypatch)
    _mock_dlpar_authorization(mock_hmc)
    _mock_owning_system_discovery(mock_hmc)
    route = mock_hmc.post(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=LPAR_ENTRY)
    )
    read = AsyncMock(return_value="")
    with patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read):
        result = tool(LPAR_UUID, LparResources(desired_procs=1.0, desired_memory=2048))

    assert result["Resource"]["PartitionName"] == "lpar1"
    assert route.called
    # The token was read against the system the walk actually found it on.
    assert read.await_args.args[1:] == ("sys1", "lpar1")


@pytest.mark.parametrize("tool", [hmc_dlpar_proc, hmc_dlpar_mem])
def test_dlpar_without_a_system_selector_refuses_a_foreign_owner(
    monkeypatch, mock_hmc, tool
):
    """Discovery feeds the guard, so the default shape is guarded too."""
    _hmc_env(monkeypatch)
    monkeypatch.setenv("HMC_AGENT_ID", "alice")
    _mock_dlpar_authorization(mock_hmc)
    _mock_owning_system_discovery(mock_hmc)
    route = mock_hmc.post(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=LPAR_ENTRY)
    )
    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value="[hmcpctl owner:bob created:2026-08-14]"),
        ),
        pytest.raises(PermissionError, match="ownership_override=true"),
    ):
        tool(LPAR_UUID, LparResources(desired_procs=1.0, desired_memory=2048))
    assert not route.called


@pytest.mark.parametrize("tool", [hmc_dlpar_proc, hmc_dlpar_mem])
def test_dlpar_override_without_a_selector_needs_no_discovery(
    monkeypatch, mock_hmc, tool
):
    """The operator's approved exception is not blocked by fleet discovery.

    No fleet route is registered, so respx fails the test if the tool tries to
    walk the fleet — the regression this asserts against. The partition read
    that names the audit record, and the write's own read, are the only lookups
    the override pays for.
    """
    _hmc_env(monkeypatch)
    monkeypatch.setenv("HMC_AGENT_ID", "alice")
    mock_hmc.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=LPAR_ENTRY, headers={"ETag": "etag-1"})
    )
    route = mock_hmc.post(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=LPAR_ENTRY)
    )
    read = AsyncMock(return_value="[hmcpctl owner:bob created:2026-08-14]")
    with patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read):
        tool(
            LPAR_UUID,
            LparResources(desired_procs=1.0, desired_memory=2048),
            ownership_override=True,
        )
    read.assert_not_awaited()
    assert route.called


def test_dlpar_proc_refuses_a_foreign_owned_partition(monkeypatch, mock_hmc):
    """ADR 0092 §3.2: the tool reaches the guard, not just the operation."""
    _hmc_env(monkeypatch)
    monkeypatch.setenv("HMC_AGENT_ID", "alice")
    _mock_dlpar_authorization(mock_hmc)
    route = mock_hmc.post(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=LPAR_ENTRY)
    )
    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value="[hmcpctl owner:bob created:2026-08-14]"),
        ),
        pytest.raises(PermissionError, match="ownership_override=true"),
    ):
        hmc_dlpar_proc(
            LPAR_UUID,
            LparResources(desired_procs=1.0),
            system_name_or_uuid=SYSTEM_UUID,
        )
    assert not route.called


def test_dlpar_mem_ownership_override_reaches_the_write(monkeypatch, mock_hmc):
    """The tool threads ADR 0092 §5's per-call override into the operation."""
    _hmc_env(monkeypatch)
    monkeypatch.setenv("HMC_AGENT_ID", "alice")
    _mock_dlpar_authorization(mock_hmc)
    route = mock_hmc.post(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").mock(
        return_value=httpx.Response(200, text=LPAR_ENTRY)
    )
    read = AsyncMock(return_value="[hmcpctl owner:bob created:2026-08-14]")
    with patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read):
        hmc_dlpar_mem(
            LPAR_UUID,
            LparResources(desired_memory=4096),
            system_name_or_uuid=SYSTEM_UUID,
            ownership_override=True,
        )
    read.assert_not_awaited()
    assert route.called


# ---------------------------------------------------------------------- #
# Managed-system / VIOS power
# ---------------------------------------------------------------------- #


def test_power_on_system_submits_job(monkeypatch, mock_hmc):
    """hmc_power_on_system PUTs a PowerOn job to the ManagedSystem."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/do/PowerOn").mock(
        return_value=httpx.Response(202, text=JOB_ENTRY)
    )
    result = hmc_power_on_system(SYSTEM_UUID)
    body = route.calls.last.request.content.decode()
    assert "PowerOn</OperationName>" in body and "ManagedSystem" in body
    assert result["Resource"]["JobID"] == JOB_ID


def test_power_off_system_submits_job(monkeypatch, mock_hmc):
    """hmc_power_off_system PUTs a PowerOff job with the immediate flag."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/do/PowerOff").mock(
        return_value=httpx.Response(202, text=JOB_ENTRY)
    )
    hmc_power_off_system(SYSTEM_UUID, immediate=True)
    body = route.calls.last.request.content.decode()
    assert "PowerOff</OperationName>" in body
    assert '<ParameterName kb="ROR" kxe="false">immediate</ParameterName>' in body
    assert '<ParameterValue kb="CUR" kxe="false">true</ParameterValue>' in body


def test_power_on_vios_submits_job(monkeypatch, mock_hmc):
    """hmc_power_on_vios PUTs a PowerOn job to the VirtualIOServer."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/do/PowerOn").mock(
        return_value=httpx.Response(202, text=JOB_ENTRY)
    )
    result = hmc_power_on_vios(VIOS_UUID)
    body = route.calls.last.request.content.decode()
    assert "PowerOn</OperationName>" in body and "VirtualIOServer" in body
    assert result["Resource"]["JobID"] == JOB_ID


def test_power_off_vios_submits_job(monkeypatch, mock_hmc):
    """hmc_power_off_vios PUTs a PowerOff job, graceful by default."""
    _hmc_env(monkeypatch)
    route = mock_hmc.put(f"/rest/api/uom/VirtualIOServer/{VIOS_UUID}/do/PowerOff").mock(
        return_value=httpx.Response(202, text=JOB_ENTRY)
    )
    hmc_power_off_vios(VIOS_UUID)
    body = route.calls.last.request.content.decode()
    assert "PowerOff</OperationName>" in body
    # Graceful shutdown omits the immediate parameter entirely.
    assert "immediate" not in body


def test_modify_system_builds_xml(monkeypatch, mock_hmc):
    """hmc_modify_system maps its args into the ManagedSystem document."""
    _hmc_env(monkeypatch)
    route = mock_hmc.post(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}").mock(
        return_value=httpx.Response(
            200, text=SYSTEM_ENTRY.format(uuid=SYSTEM_UUID, name="newsysname")
        )
    )
    result = hmc_modify_system(
        SYSTEM_UUID,
        new_name="newsysname",
        power_off_policy=1,
        power_on_lpar_start_policy="autostart",
        mem_mirroring_mode="sys_firmware_only",
    )
    body = route.calls.last.request.content.decode()
    assert "newsysname</SystemName>" in body
    assert '<PowerOffPolicy kb="CUD" kxe="false">1</PowerOffPolicy>' in body
    assert (
        '<PowerOnLparStartPolicy kb="CUD" kxe="false">autostart</PowerOnLparStartPolicy>'
        in body
    )
    assert (
        '<MemoryMirroringMode kb="CUD" kxe="false">sys_firmware_only</MemoryMirroringMode>'
        in body
    )
    assert result["Resource"]["SystemName"] == "newsysname"


def test_modify_system_invalid_policy_rejected_before_http(monkeypatch, mock_hmc):
    """An invalid power_off_policy raises ValueError before any HTTP call."""
    _hmc_env(monkeypatch)
    route = mock_hmc.post(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}")
    with pytest.raises(ValueError, match="power_off_policy"):
        hmc_modify_system(SYSTEM_UUID, power_off_policy=2)
    assert not route.called


@pytest.mark.parametrize("tool", ["hmc_dlpar_proc", "hmc_modify_lpar"])
@pytest.mark.parametrize("weight", [True, False, "0", "64", 0.0, 64.5])
def test_sharing_mcp_input_is_strict(tool, weight, monkeypatch):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from hmcpctl.authorization.access_policy import compile_access_policy
    from hmcpctl.server import TOOL_SECURITY, create_mcp

    app = create_mcp(
        compile_access_policy(
            {
                "policies": {
                    "test": {
                        "grants": [
                            {
                                "tools": [tool],
                                "connections": ["<default>"],
                                "targets": "all-targets",
                            }
                        ]
                    }
                }
            },
            "test",
            TOOL_SECURITY,
            "test.toml",
        )
    )
    client = Mock(side_effect=AssertionError("Invalid weight reached client"))
    monkeypatch.setattr("hmcpctl.server_tools.lpar.lifecycle.with_client", client)
    monkeypatch.setenv("HMC_HOST", "hmc.test")

    async def call():
        async with Client(app) as mcp:
            await mcp.call_tool(
                tool,
                {
                    "lpar_name_or_uuid": LPAR_UUID,
                    "resources": {"uncapped_weight": weight},
                },
            )

    with pytest.raises(ToolError, match="uncapped_weight"):
        asyncio.run(call())
    client.assert_not_called()


@pytest.mark.parametrize(
    "tool,operation",
    [("hmc_dlpar_proc", "set_lpar_processors"), ("hmc_modify_lpar", "modify_lpar")],
)
def test_sharing_mcp_input_keeps_zero(tool, operation, monkeypatch):
    from fastmcp import Client

    from hmcpctl.authorization.access_policy import compile_access_policy
    from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult
    from hmcpctl.server import TOOL_SECURITY, create_mcp
    from hmcpctl.server_tools.lpar import lifecycle

    outcome = {"UUID": LPAR_UUID, "Resource": {}, "workflow_completed": False}
    result = (
        outcome
        if tool == "hmc_dlpar_proc"
        else LparPcieWorkflowResult(False, False, outcome, None, (), ())
    )
    delegate = AsyncMock(return_value=result)
    monkeypatch.setattr(lifecycle, operation, delegate)
    monkeypatch.setattr(
        lifecycle, "with_client", lambda work, **kwargs: asyncio.run(work(object()))
    )
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    app = create_mcp(
        compile_access_policy(
            {
                "policies": {
                    "test": {
                        "grants": [
                            {
                                "tools": [tool],
                                "connections": ["<default>"],
                                "targets": "all-targets",
                            }
                        ]
                    }
                }
            },
            "test",
            TOOL_SECURITY,
            "test.toml",
        )
    )

    async def call():
        async with Client(app) as mcp:
            return await mcp.call_tool(
                tool,
                {"lpar_name_or_uuid": LPAR_UUID, "resources": {"uncapped_weight": 0}},
            )

    served = asyncio.run(call())
    assert served.structured_content is not None
    assert delegate.await_args.args[2].uncapped_weight == 0
    assert type(delegate.await_args.args[2].uncapped_weight) is int


@pytest.mark.parametrize("combined", [False, True])
def test_sharing_mcp_result_propagation(combined, monkeypatch):
    from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
    from hmcpctl.server_tools.lpar import lifecycle

    outcome = {
        "UUID": LPAR_UUID,
        "Resource": {},
        "completion_scope": "current-configuration",
        "outcome": "partial",
        "workflow_completed": False,
        "submission_status": "accepted",
        "configuration_verification": "verified",
        "runtime_verification": "unverified",
        "before": {"weight": 64},
        "after": {"weight": 0},
        "expected": {
            "configured": "uncapped",
            "effective_mode": "uncapped",
            "weight": 0,
        },
        "submission_error": None,
        "read_error": "unavailable",
        "change_location": {},
        "warnings": ["inspect before retry"],
    }
    workflow = LparPcieWorkflowResult(
        False,
        False,
        outcome,
        None,
        (
            WorkflowStep("resources", "error", outcome),
            WorkflowStep("vnic[0]", "skipped"),
        ),
        ("inspect before retry",),
    )
    expected = workflow if combined else outcome
    delegate = AsyncMock(return_value=expected)
    monkeypatch.setattr(
        lifecycle, "modify_lpar" if combined else "set_lpar_processors", delegate
    )
    monkeypatch.setattr(
        lifecycle, "with_client", lambda work, **kwargs: asyncio.run(work(object()))
    )
    tool = lifecycle.hmc_modify_lpar if combined else lifecycle.hmc_dlpar_proc
    result = tool(
        LPAR_UUID,
        LparResources(uncapped_weight=0),
        system_name_or_uuid=SYSTEM_UUID,
        ownership_override=True,
        profile="selected",
    )
    assert result is expected
    assert delegate.await_args.kwargs == {
        "ownership_override": True,
        "system_name_or_uuid": SYSTEM_UUID,
    }
    if combined:
        assert asdict(result)["steps"][0]["result"] == outcome
