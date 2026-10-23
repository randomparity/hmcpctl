"""Declarative PCIe assignment workflow tests."""

from contextlib import ExitStack
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from conftest import live_fixture

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar import LparResources
from hmcpctl.operations.lpar.assignments import (
    DedicatedPcieAssignment,
    LparPcieAssignments,
    SriovLogicalPortAssignment,
    VnicAssignment,
    _analyze_assignment_requests,
    apply_lpar_pcie_assignments,
    apply_validated_lpar_pcie_assignments,
    prevalidate_lpar_pcie_assignments,
)
from hmcpctl.operations.lpar.core import LparCreation
from hmcpctl.operations.lpar.workflows import create_lpar
from hmcpctl.operations.virtualization.pcie import (
    InventoryResult,
    InventorySelector,
    PcieAssignmentPartialError,
    PcieAssignmentUnavailableError,
    SriovAdapter,
    SriovLogicalPortCapabilityError,
    list_sriov_physical_ports,
    require_admitted_environment,
)
from hmcpctl.operations.virtualization.vnic import VnicBackingSelector
from hmcpctl.ssh.transport import HMCCLIError


def _sriov(logical: str = "27004001") -> SriovLogicalPortAssignment:
    return SriovLogicalPortAssignment("default_profile", "1", "1", logical, Decimal(2))


def _vnic() -> VnicAssignment:
    return VnicAssignment(
        VnicBackingSelector("vios-a", "100", "1", "1", Decimal(3)), 42
    )


def _physical_row(state: str) -> dict[str, str]:
    return {
        "adapter_id": "1",
        "phys_port_id": "1",
        "phys_port_type": "eth",
        "phys_port_loc": "U-T1",
        "state": state,
        "config_logical_ports": "0",
        "phys_port_max_logical_ports": "60",
        "curr_eth_logical_ports": "0",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "availability"), [("1", "up"), ("0", "down")])
async def test_prevalidation_uses_normalized_physical_port_availability(
    state: str, availability: str
) -> None:
    config = HMCConfig.from_mapping({"host": "h", "user": "u", "password": "p"})
    rows = AsyncMock(return_value=[_physical_row(state)])
    adapter = InventoryResult(
        "sriov_adapter",
        "available",
        "sys",
        InventorySelector("1"),
        [SriovAdapter("sys", "1", "sriov", "1", None, None, None, None)],
        None,
    )
    logical = InventoryResult(
        "sriov_logical_port", "available", "sys", InventorySelector("1", "1"), [], None
    )
    with (
        patch(
            "hmcpctl.operations.virtualization.pcie._system_name",
            AsyncMock(return_value="sys"),
        ),
        patch(
            "hmcpctl.operations.virtualization.pcie.require_admitted_environment",
            AsyncMock(),
        ),
        patch(
            "hmcpctl.operations.virtualization.pcie.require_sriov_read_environment",
            AsyncMock(),
        ),
        patch(
            "hmcpctl.operations.lpar.assignments.require_admitted_environment",
            AsyncMock(),
        ),
        patch(
            "hmcpctl.operations.virtualization.pcie.list_sriov_physical_port_rows", rows
        ),
        patch(
            "hmcpctl.operations.lpar.assignments.list_sriov_adapters",
            AsyncMock(return_value=adapter),
        ),
        patch(
            "hmcpctl.operations.lpar.assignments.list_sriov_logical_ports",
            AsyncMock(return_value=logical),
        ),
        patch(
            "hmcpctl.operations.lpar.assignments._existing_capacity",
            AsyncMock(return_value=Decimal()),
        ),
    ):
        ports = await list_sriov_physical_ports(
            SimpleNamespace(config=config), "sys", "1", "1"
        )
        assert ports.items[0].availability == availability
        assignments = LparPcieAssignments(sriov=(_sriov(),))
        if availability == "up":
            await prevalidate_lpar_pcie_assignments(
                SimpleNamespace(config=config), "sys", assignments
            )
        else:
            with pytest.raises(ValueError, match="physical port.*not healthy"):
                await prevalidate_lpar_pcie_assignments(
                    SimpleNamespace(config=config), "sys", assignments
                )


def test_request_analysis_returns_capacity_and_unique_vios_requirements() -> None:
    vnic = _vnic()
    capacities, vios_identities = _analyze_assignment_requests(
        LparPcieAssignments(sriov=(_sriov(),), vnics=(vnic,))
    )

    assert capacities == {("1", "1"): (Decimal(2), Decimal(3))}
    assert vios_identities == {("vios-a", "100")}


def _dedicated(drc: str = "21010020") -> LparPcieAssignments:
    return LparPcieAssignments(
        dedicated=(DedicatedPcieAssignment("default_profile", drc),)
    )


def _environment(model: str) -> AsyncMock:
    return AsyncMock(
        return_value=("version= Version: 10\n Release: 3\n Service Pack: 1060\n", model)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["9009-42A", "9105-22A"])
async def test_dedicated_request_outside_the_envelope_fails_before_creation(
    model: str,
) -> None:
    config = HMCConfig.from_mapping({"host": "h", "user": "u", "password": "p"})
    with (
        patch(
            "hmcpctl.operations.lpar.assignments.resolve_ssh_names",
            AsyncMock(return_value=("sys", None)),
        ),
        # The inventory stub stands the gate down; this test needs the real one.
        patch(
            "hmcpctl.operations.lpar.assignments.require_admitted_environment",
            require_admitted_environment,
        ),
        patch(
            "hmcpctl.operations.virtualization.pcie.read_sriov_environment",
            _environment(model),
        ),
        pytest.raises(PcieAssignmentUnavailableError, match="V10R3 M1060"),
    ):
        await prevalidate_lpar_pcie_assignments(
            SimpleNamespace(config=config), "sys", _dedicated()
        )


@pytest.mark.asyncio
async def test_dedicated_request_inside_the_envelope_prevalidates() -> None:
    config = HMCConfig.from_mapping({"host": "h", "user": "u", "password": "p"})
    environment = _environment("8375-42A")
    with (
        patch(
            "hmcpctl.operations.lpar.assignments.resolve_ssh_names",
            AsyncMock(return_value=("sys-resolved", None)),
        ),
        patch(
            "hmcpctl.operations.virtualization.pcie.read_sriov_environment", environment
        ),
    ):
        await prevalidate_lpar_pcie_assignments(
            SimpleNamespace(config=config), "sys", _dedicated()
        )
    environment.assert_awaited_once_with(config, "sys-resolved")


@pytest.mark.asyncio
@pytest.mark.parametrize("drc", ["2101002a", "553713664", "2101/020", " "])
async def test_malformed_dedicated_slot_fails_before_any_hmc_call(drc: str) -> None:
    hmc = AsyncMock()
    with pytest.raises(ValueError, match="drc_index"):
        await prevalidate_lpar_pcie_assignments(hmc, "sys", _dedicated(drc))
    assert hmc.mock_calls == []


@pytest.mark.asyncio
async def test_duplicate_dedicated_request_fails_before_any_hmc_call() -> None:
    hmc = AsyncMock()
    request = DedicatedPcieAssignment("default_profile", "21010020")
    with pytest.raises(ValueError, match="duplicate dedicated"):
        await prevalidate_lpar_pcie_assignments(
            hmc, "sys", LparPcieAssignments(dedicated=(request, request))
        )
    assert hmc.mock_calls == []


@pytest.mark.asyncio
async def test_unverified_dedicated_change_is_an_error_step_that_skips_the_rest() -> (
    None
):
    assignments = LparPcieAssignments(
        dedicated=(DedicatedPcieAssignment("default_profile", "21010020"),),
        sriov=(_sriov(),),
    )
    sriov = AsyncMock()
    with (
        patch(
            "hmcpctl.operations.lpar.assignments.assign_dedicated_pcie_slot",
            AsyncMock(side_effect=PcieAssignmentPartialError("readback mismatch")),
        ),
        patch("hmcpctl.operations.lpar.assignments.assign_sriov_logical_port", sriov),
    ):
        result = await apply_validated_lpar_pcie_assignments(
            AsyncMock(), "sys", "lpar", assignments
        )
    assert [(step.step, step.status) for step in result.steps] == [
        ("dedicated[0]", "error"),
        ("sriov[0]", "skipped"),
    ]
    assert result.steps[0].result == "readback mismatch"
    sriov.assert_not_awaited()


@pytest.mark.asyncio
async def test_conflicting_duplicate_logical_port_fails_closed() -> None:
    assignments = LparPcieAssignments(
        sriov=(
            _sriov(),
            SriovLogicalPortAssignment(
                "default_profile", "1", "2", "27004001", Decimal(3)
            ),
        )
    )
    with pytest.raises(ValueError, match="conflicting duplicate"):
        await prevalidate_lpar_pcie_assignments(AsyncMock(), "sys", assignments)


@pytest.mark.asyncio
async def test_duplicate_vnic_request_fails_before_inventory() -> None:
    item = _vnic()
    with pytest.raises(ValueError, match="duplicate vNIC"):
        await prevalidate_lpar_pcie_assignments(
            AsyncMock(), "sys", LparPcieAssignments(vnics=(item, item))
        )


@pytest.mark.asyncio
async def test_structural_selector_character_fails_before_inventory() -> None:
    request = SriovLogicalPortAssignment(
        "default_profile", "1,2", "1", "27004001", Decimal(2)
    )
    with pytest.raises(ValueError, match="comma"):
        await prevalidate_lpar_pcie_assignments(
            AsyncMock(), "sys", LparPcieAssignments(sriov=(request,))
        )


@pytest.mark.asyncio
async def test_dry_run_preserves_stable_assignment_order() -> None:
    assignments = LparPcieAssignments(sriov=(_sriov(),), vnics=(_vnic(),))
    with patch(
        "hmcpctl.operations.lpar.assignments.prevalidate_lpar_pcie_assignments",
        AsyncMock(),
    ):
        result = await apply_validated_lpar_pcie_assignments(
            AsyncMock(), "sys", "lpar", assignments, dry_run=True
        )
    assert [step.step for step in result.steps] == ["sriov[0]", "vnic[0]"]
    assert {step.status for step in result.steps} == {"dry_run"}


@pytest.mark.asyncio
async def test_prevalidated_post_create_path_does_not_repeat_inventory() -> None:
    validation = AsyncMock(side_effect=RuntimeError("concurrent inventory change"))
    with patch(
        "hmcpctl.operations.lpar.assignments.prevalidate_lpar_pcie_assignments",
        validation,
    ):
        result = await apply_validated_lpar_pcie_assignments(
            AsyncMock(),
            "sys",
            "created-lpar",
            LparPcieAssignments(),
        )
    validation.assert_not_awaited()
    assert result.workflow_completed is True


@pytest.mark.asyncio
async def test_public_apply_cannot_bypass_validation() -> None:
    validation = AsyncMock(side_effect=ValueError("unsafe collection"))
    with (
        patch(
            "hmcpctl.operations.lpar.assignments.prevalidate_lpar_pcie_assignments",
            validation,
        ),
        pytest.raises(ValueError, match="unsafe collection"),
    ):
        await apply_lpar_pcie_assignments(
            AsyncMock(), "lpar", LparPcieAssignments(), system_name_or_uuid="sys"
        )
    validation.assert_awaited_once()


@pytest.mark.asyncio
async def test_first_assignment_failure_skips_remaining_steps() -> None:
    assignments = LparPcieAssignments(sriov=(_sriov(),), vnics=(_vnic(),))
    sriov = AsyncMock(side_effect=SriovLogicalPortCapabilityError("stale inventory"))
    vnic = AsyncMock()
    with (
        patch(
            "hmcpctl.operations.lpar.assignments.prevalidate_lpar_pcie_assignments",
            AsyncMock(),
        ),
        patch("hmcpctl.operations.lpar.assignments.assign_sriov_logical_port", sriov),
        patch("hmcpctl.operations.lpar.assignments.add_vnic", vnic),
    ):
        result = await apply_lpar_pcie_assignments(
            AsyncMock(), "lpar", assignments, system_name_or_uuid="sys"
        )
    assert [(step.step, step.status) for step in result.steps] == [
        ("sriov[0]", "error"),
        ("vnic[0]", "skipped"),
    ]
    vnic.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [ValueError("invalid backing"), PermissionError("foreign owner")]
)
async def test_later_preflight_failure_preserves_prior_assignment(
    failure: Exception,
) -> None:
    assignments = LparPcieAssignments(sriov=(_sriov(),), vnics=(_vnic(),))
    with (
        patch(
            "hmcpctl.operations.lpar.assignments.prevalidate_lpar_pcie_assignments",
            AsyncMock(),
        ),
        patch(
            "hmcpctl.operations.lpar.assignments.assign_sriov_logical_port",
            AsyncMock(return_value={"assigned": True}),
        ),
        patch(
            "hmcpctl.operations.lpar.assignments.add_vnic",
            AsyncMock(side_effect=failure),
        ),
    ):
        result = await apply_lpar_pcie_assignments(
            AsyncMock(), "lpar", assignments, system_name_or_uuid="sys"
        )

    assert [step.status for step in result.steps] == ["ok", "error"]
    assert result.steps[0].result == {"assigned": True}
    assert result.steps[1].result == str(failure)
    assert result.workflow_completed is False


@pytest.mark.asyncio
async def test_assignment_programming_defect_propagates() -> None:
    assignments = LparPcieAssignments(sriov=(_sriov(),))
    with (
        patch(
            "hmcpctl.operations.lpar.assignments.prevalidate_lpar_pcie_assignments",
            AsyncMock(),
        ),
        patch(
            "hmcpctl.operations.lpar.assignments.assign_sriov_logical_port",
            AsyncMock(side_effect=TypeError("bad collaborator")),
        ),
        pytest.raises(TypeError, match="bad collaborator"),
    ):
        await apply_lpar_pcie_assignments(
            AsyncMock(), "lpar", assignments, system_name_or_uuid="sys"
        )


@pytest.mark.asyncio
async def test_success_composes_existing_operations_in_order() -> None:
    assignments = LparPcieAssignments(sriov=(_sriov(),), vnics=(_vnic(),))
    calls: list[str] = []

    async def assign(*args, **kwargs):
        calls.append("sriov")
        return {"changed": True}

    async def add(*args, **kwargs):
        calls.append("vnic")
        return {"changed": True}

    with (
        patch(
            "hmcpctl.operations.lpar.assignments.prevalidate_lpar_pcie_assignments",
            AsyncMock(),
        ),
        patch("hmcpctl.operations.lpar.assignments.assign_sriov_logical_port", assign),
        patch("hmcpctl.operations.lpar.assignments.add_vnic", add),
    ):
        result = await apply_lpar_pcie_assignments(
            AsyncMock(), "lpar", assignments, system_name_or_uuid="sys"
        )
    assert calls == ["sriov", "vnic"]
    assert result.workflow_completed is True


def _granularity_inventory(granularity: str | None) -> ExitStack:
    """Patch SR-IOV and VIOS inventory so only the port granularity varies."""
    row = _physical_row("1")
    if granularity is not None:
        row["min_eth_capacity_granularity"] = granularity
    adapter = InventoryResult(
        "sriov_adapter",
        "available",
        "sys",
        InventorySelector("1"),
        [SriovAdapter("sys", "1", "sriov", "1", None, None, None, None)],
        None,
    )
    logical = InventoryResult(
        "sriov_logical_port", "available", "sys", InventorySelector("1", "1"), [], None
    )
    pcie = "hmcpctl.operations.virtualization.pcie"
    assignments = "hmcpctl.operations.lpar.assignments"
    vios = {"name": "vios-a", "lpar_id": "100", "lpar_env": "vioserver"}
    stack = ExitStack()
    for target, mock in (
        (f"{pcie}._system_name", AsyncMock(return_value="sys")),
        (f"{pcie}.require_admitted_environment", AsyncMock()),
        (f"{pcie}.require_sriov_read_environment", AsyncMock()),
        (f"{assignments}.require_admitted_environment", AsyncMock()),
        (f"{pcie}.list_sriov_physical_port_rows", AsyncMock(return_value=[row])),
        (f"{assignments}.list_sriov_adapters", AsyncMock(return_value=adapter)),
        (f"{assignments}.list_sriov_logical_ports", AsyncMock(return_value=logical)),
        (f"{assignments}._existing_capacity", AsyncMock(return_value=Decimal())),
        (f"{assignments}.read_vios_identity", AsyncMock(return_value=vios)),
    ):
        stack.enter_context(patch(target, mock))
    return stack


def _sriov_at(capacity: str, logical: str = "27004001") -> SriovLogicalPortAssignment:
    return SriovLogicalPortAssignment(
        "default_profile", "1", "1", logical, Decimal(capacity)
    )


def _vnic_at(capacity: str) -> VnicAssignment:
    return VnicAssignment(
        VnicBackingSelector("vios-a", "100", "1", "1", Decimal(capacity)), 42
    )


async def _prevalidate(assignments: LparPcieAssignments) -> None:
    config = HMCConfig.from_mapping({"host": "h", "user": "u", "password": "p"})
    await prevalidate_lpar_pcie_assignments(
        SimpleNamespace(config=config), "sys", assignments
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "assignments",
    [
        pytest.param(LparPcieAssignments(sriov=(_sriov_at("7.5"),)), id="logical-port"),
        pytest.param(LparPcieAssignments(vnics=(_vnic_at("7.5"),)), id="vnic-backing"),
        pytest.param(
            LparPcieAssignments(sriov=(_sriov_at("2.5"), _sriov_at("2.5", "27004002"))),
            id="items-whose-sum-is-on-granularity",
        ),
    ],
)
async def test_prevalidation_refuses_each_item_off_the_port_granularity(
    assignments: LparPcieAssignments,
) -> None:
    with (
        _granularity_inventory("1.0"),
        pytest.raises(
            ValueError,
            match=r"capacity_percent [27]\.5 is not a multiple of the physical port's "
            r"capacity granularity 1\.0%",
        ),
    ):
        await _prevalidate(assignments)


@pytest.mark.asyncio
@pytest.mark.parametrize("granularity", ["2.0", None, "", "null"])
async def test_prevalidation_admits_on_granularity_or_unreported_granularity(
    granularity: str | None,
) -> None:
    capacity = "4" if granularity == "2.0" else "7.5"
    with _granularity_inventory(granularity):
        await _prevalidate(
            LparPcieAssignments(
                sriov=(_sriov_at(capacity),), vnics=(_vnic_at(capacity),)
            )
        )


@pytest.mark.asyncio
async def test_prevalidation_rejects_malformed_port_granularity() -> None:
    with (
        _granularity_inventory("abc"),
        pytest.raises(
            HMCCLIError, match="malformed physical-port capacity granularity"
        ),
    ):
        await _prevalidate(LparPcieAssignments(sriov=(_sriov_at("2"),)))


@pytest.mark.asyncio
async def test_create_refuses_off_granularity_capacity_before_creating_the_lpar() -> (
    None
):
    create = AsyncMock()
    config = HMCConfig.from_mapping({"host": "h", "user": "u", "password": "p"})
    with (
        _granularity_inventory("1.0"),
        patch("hmcpctl.operations.lpar.workflows.create_and_stamp_lpar", create),
        pytest.raises(ValueError, match="not a multiple"),
    ):
        await create_lpar(
            SimpleNamespace(config=config),
            "sys",
            LparCreation("new-lpar", "AIX/Linux", LparResources()),
            LparPcieAssignments(sriov=(_sriov_at("7.5"),)),
        )
    create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "assignments",
    [
        pytest.param(LparPcieAssignments(sriov=(_sriov_at("2"),)), id="logical-port"),
        pytest.param(LparPcieAssignments(vnics=(_vnic_at("2"),)), id="vnic-backing"),
    ],
)
async def test_create_refuses_sriov_items_outside_the_mutation_envelope(
    assignments: LparPcieAssignments,
) -> None:
    # ADR 0183 admits V11R2 SR-IOV reads on a POWER9 9009-42A, but no capture
    # holds a V11R2 assignment, so the request fails before an LPAR exists.
    create = AsyncMock()
    config = HMCConfig.from_mapping({"host": "h", "user": "u", "password": "p"})
    environment = AsyncMock(
        return_value=(
            live_fixture("cli-lshmc-version-v11r2")["stdout"],
            live_fixture("cli-type-model-v11r2-p9-9009-42a")["stdout"].strip(),
        )
    )
    with (
        _granularity_inventory("1.0"),
        patch(
            "hmcpctl.operations.virtualization.pcie.read_sriov_environment",
            environment,
        ),
        patch(
            "hmcpctl.operations.lpar.assignments.resolve_ssh_names",
            AsyncMock(return_value=("sys", None)),
        ),
        # The inventory stub stands the gate down; this test needs the real one.
        patch(
            "hmcpctl.operations.lpar.assignments.require_admitted_environment",
            require_admitted_environment,
        ),
        patch("hmcpctl.operations.lpar.workflows.create_and_stamp_lpar", create),
        pytest.raises(SriovLogicalPortCapabilityError, match="8375-42A"),
    ):
        await create_lpar(
            SimpleNamespace(config=config),
            "sys",
            LparCreation("new-lpar", "AIX/Linux", LparResources()),
            assignments,
        )
    create.assert_not_awaited()
