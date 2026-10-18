"""Tests for ownership token stamping in direct LPAR creation (issue #132)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import httpx
import pytest
import respx
from conftest import mock_system_summaries

from hmcpctl.operations.lpar.assignments import (
    LparPcieAssignments,
    SriovLogicalPortAssignment,
)
from hmcpctl.operations.virtualization.pcie import InventorySelector
from hmcpctl.server_tools.lpar.lifecycle_create import hmc_create_lpar

BASE = "https://hmc.test"
SYSTEM_UUID = "aaaa0000-0000-0000-0000-000000000001"
LPAR_UUID = "bbbb0000-0000-0000-0000-000000000001"

LOGON_XML = """<?xml version="1.0" encoding="UTF-8"?>
<LogonResponse xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/">
  <X-API-Session>tok</X-API-Session>
</LogonResponse>"""

EMPTY_FEED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom"/>"""

LPAR_ENTRY = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:{uuid}</id>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <PartitionName>{name}</PartitionName>
      <PartitionState>not activated</PartitionState>
    </LogicalPartition>
  </content>
</entry>"""


def _env(monkeypatch, agent_id: str | None = "test-agent"):
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "u")
    monkeypatch.setenv("HMC_PASSWORD", "p")
    if agent_id:
        monkeypatch.setenv("HMC_AGENT_ID", agent_id)


def _setup_mock(router):
    """Register the standard logon/logoff + system UUID resolve + name check stubs."""
    router.put("/rest/api/web/Logon").mock(
        return_value=httpx.Response(200, text=LOGON_XML)
    )
    router.delete("/rest/api/web/Logon").mock(return_value=httpx.Response(204))
    # Name-uniqueness check on the target system — no existing LPAR
    router.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(200, text=EMPTY_FEED)
    )
    # System UUID lookup (resolve_system_uuid called with a UUID → direct GET)
    router.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}").mock(
        return_value=httpx.Response(
            200,
            text=f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:{SYSTEM_UUID}</id>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <ManagedSystem xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <SystemName>server1</SystemName>
      <State>operating</State>
    </ManagedSystem>
  </content>
</entry>""",
        )
    )
    # The empty name-check feed reads the system's State from quick/All
    mock_system_summaries(router, (SYSTEM_UUID, "server1", "operating"))
    # REST LPAR create
    router.put(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(
            201,
            text=LPAR_ENTRY.format(uuid=LPAR_UUID, name="test-lpar"),
        )
    )


# ---------------------------------------------------------------------- #
# Stamping success
# ---------------------------------------------------------------------- #


def test_create_lpar_ownership_stamped_true(monkeypatch):
    """When stamp succeeds, result has ownership_stamped=True and empty warnings."""
    _env(monkeypatch)
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        _setup_mock(router)
        with patch(
            "hmcpctl.operations.lpar.ownership.stamp_lpar_ownership",
            new=AsyncMock(return_value="[hmcpctl owner:test-agent created:2026-08-13]"),
        ):
            result = hmc_create_lpar(
                system_name_or_uuid=SYSTEM_UUID,
                name="test-lpar",
                apply_partition_profile=False,
            )

    assert result.resource_created is True
    assert result.ownership_stamped is True
    assert result.warnings == ()
    # The lpar key carries the created partition entry
    assert result.lpar["Resource"]["PartitionName"] == "test-lpar"


def test_create_lpar_applies_validated_sriov_assignment(monkeypatch):
    _env(monkeypatch)
    assignment = SriovLogicalPortAssignment("default", "1", "2", "3", Decimal(5))
    adapter = SimpleNamespace(
        capability="available",
        items=[SimpleNamespace(mode="sriov", availability="1")],
        system="server1",
    )
    port = SimpleNamespace(
        capability="available",
        items=[
            SimpleNamespace(
                availability="up", minimum_capacity_granularity_percent=None
            )
        ],
    )
    logical = SimpleNamespace(capability="available")
    assigned = AsyncMock(return_value={"status": "ok"})
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        _setup_mock(router)
        with (
            patch(
                "hmcpctl.operations.lpar.ownership.stamp_lpar_ownership",
                new=AsyncMock(return_value="stamp"),
            ),
            patch(
                "hmcpctl.operations.lpar.assignments.list_sriov_adapters",
                AsyncMock(return_value=adapter),
            ),
            patch(
                "hmcpctl.operations.lpar.assignments.list_sriov_physical_ports",
                AsyncMock(return_value=port),
            ),
            patch(
                "hmcpctl.operations.lpar.assignments.list_sriov_logical_ports",
                AsyncMock(return_value=logical),
            ),
            patch(
                "hmcpctl.operations.lpar.assignments._existing_capacity",
                AsyncMock(return_value=Decimal()),
            ),
            patch(
                "hmcpctl.operations.lpar.assignments.require_admitted_environment",
                AsyncMock(),
            ),
            patch(
                "hmcpctl.operations.lpar.assignments.assign_sriov_logical_port",
                assigned,
            ),
        ):
            result = hmc_create_lpar(
                SYSTEM_UUID,
                "test-lpar",
                assignments=LparPcieAssignments(sriov=(assignment,)),
                apply_partition_profile=False,
            )

    assigned.assert_awaited_once_with(
        ANY,
        "test-lpar",
        InventorySelector("1", "2", "3"),
        Decimal(5),
        system_name_or_uuid=SYSTEM_UUID,
        profile_name="default",
        ownership_override=False,
    )
    assert [(step.step, step.status) for step in result.steps] == [
        ("create", "ok"),
        ("sriov[0]", "ok"),
    ]
    assert result.resource_created and result.workflow_completed
    assert result.lpar["Resource"]["PartitionName"] == "test-lpar"


# ---------------------------------------------------------------------- #
# Stamp failure
# ---------------------------------------------------------------------- #


def test_create_lpar_ownership_stamped_false_on_stamp_failure(monkeypatch):
    """When stamp returns None, ownership_stamped=False and warnings has a message."""
    _env(monkeypatch)
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        _setup_mock(router)
        with patch(
            "hmcpctl.operations.lpar.ownership.stamp_lpar_ownership",
            new=AsyncMock(return_value=None),
        ):
            result = hmc_create_lpar(
                system_name_or_uuid=SYSTEM_UUID,
                name="test-lpar",
                apply_partition_profile=False,
            )

    assert result.ownership_stamped is False
    assert len(result.warnings) == 1
    assert "stamp" in result.warnings[0].lower()
    # LPAR is still returned
    assert result.lpar is not None


# ---------------------------------------------------------------------- #
# Return shape always present (even when no agent_id set)
# ---------------------------------------------------------------------- #


def test_create_lpar_result_shape_without_agent_id(monkeypatch):
    """Without HMC_AGENT_ID, result still has ownership_stamped and warnings keys."""
    _env(monkeypatch, agent_id=None)  # no HMC_AGENT_ID
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        _setup_mock(router)
        with patch(
            "hmcpctl.operations.lpar.ownership.stamp_lpar_ownership",
            new=AsyncMock(return_value="[hmcpctl owner:hmcpctl created:2026-08-13]"),
        ):
            result = hmc_create_lpar(
                system_name_or_uuid=SYSTEM_UUID,
                name="test-lpar",
                apply_partition_profile=False,
            )

    assert result.lpar is not None
    assert result.ownership_stamped is True
    assert result.warnings == ()


def test_create_lpar_invalid_caller_token_zero_routes(monkeypatch):
    """A malformed token fails before any HMC traffic (spec guarantee 3)."""
    _env(monkeypatch)
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        _setup_mock(router)
        with pytest.raises(ValueError, match="caller_token"):
            hmc_create_lpar(
                system_name_or_uuid=SYSTEM_UUID, name="test-lpar", caller_token="a=b"
            )
        assert all(not route.called for route in router.routes)


def test_create_lpar_valid_caller_token_stamped(monkeypatch):
    """A valid token threads through to the composed ownership stamp."""
    captured: dict[str, str] = {}

    async def capture_stamp(
        config, system_name, lpar_name, *, agent_id=None, caller_token=None
    ):
        captured["description"] = (
            f"[hmcpctl owner:hmcpctl created:2026-08-21] [caller {caller_token}]"
        )
        return captured["description"]

    _env(monkeypatch)
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        _setup_mock(router)
        with patch(
            "hmcpctl.operations.lpar.ownership.stamp_lpar_ownership", new=capture_stamp
        ):
            result = hmc_create_lpar(
                system_name_or_uuid=SYSTEM_UUID,
                name="test-lpar",
                apply_partition_profile=False,
                caller_token="CHG-9",
            )
    assert result.resource_created is True
    assert result.ownership_stamped is True
    assert result.warnings == ()
    assert captured["description"].endswith("[caller CHG-9]")
