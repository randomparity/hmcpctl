"""Direct contracts for the shared LPAR creation workflow."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.assignments import (
    AssignmentResult,
    DedicatedPcieAssignment,
    LparPcieAssignments,
)
from hmcpctl.operations.lpar.core import LparCreation, LparCreationResult
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
from hmcpctl.operations.lpar.workflows import create_lpar


def _creation() -> LparCreation:
    from hmcpctl.documents.lpar import LparResources

    return LparCreation("app-lpar", "AIX/Linux", LparResources())


@pytest.mark.asyncio
async def test_create_lpar_returns_create_step_when_creation_has_no_resource(
    monkeypatch,
):
    hmc = cast(HMCClient, object())
    assignments = LparPcieAssignments()
    prevalidate = AsyncMock()
    create = AsyncMock(
        return_value=LparCreationResult(True, None, False, ("create warning",))
    )
    apply = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.prevalidate_lpar_pcie_assignments",
        prevalidate,
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.create_and_stamp_lpar", create
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.apply_validated_lpar_pcie_assignments",
        apply,
    )

    result = await create_lpar(hmc, "sys1", _creation(), assignments)

    assert result.resource_created is True
    assert result.workflow_completed is False
    assert result.steps == (WorkflowStep("create", "ok", None),)
    assert result.warnings == ("create warning",)
    prevalidate.assert_awaited_once_with(hmc, "sys1", assignments)
    apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_lpar_appends_assignment_steps(monkeypatch):
    hmc = cast(HMCClient, object())
    assignments = LparPcieAssignments()
    lpar = {"UUID": "lpar-1"}
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.prevalidate_lpar_pcie_assignments",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.create_and_stamp_lpar",
        AsyncMock(return_value=LparCreationResult(True, lpar, True, ())),
    )
    apply = AsyncMock(
        return_value=AssignmentResult(
            True, False, (WorkflowStep("vnic[0]", "ok", {"slot": 4}),)
        )
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.apply_validated_lpar_pcie_assignments",
        apply,
    )

    result = await create_lpar(hmc, "sys1", _creation(), assignments)

    assert result.workflow_completed is True
    assert result.lpar is lpar
    assert result.steps == (
        WorkflowStep("create", "ok", lpar),
        WorkflowStep("vnic[0]", "ok", {"slot": 4}),
    )
    apply.assert_awaited_once_with(hmc, "sys1", "app-lpar", assignments)


@pytest.mark.asyncio
async def test_create_lpar_skips_assignments_after_apply_error(monkeypatch):
    hmc = cast(HMCClient, object())
    assignments = LparPcieAssignments(
        dedicated=(DedicatedPcieAssignment("default_profile", "21010010"),)
    )
    lpar = {"UUID": "lpar-1"}
    apply_step = WorkflowStep("apply_profile", "error", "HSCL boom")
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.prevalidate_lpar_pcie_assignments",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.create_and_stamp_lpar",
        AsyncMock(
            return_value=LparCreationResult(True, lpar, True, ("w",), apply_step)
        ),
    )
    apply = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.apply_validated_lpar_pcie_assignments",
        apply,
    )

    result = await create_lpar(hmc, "sys1", _creation(), assignments)

    assert result.workflow_completed is False
    assert result.lpar is lpar
    assert result.steps == (
        WorkflowStep("create", "ok", lpar),
        apply_step,
        WorkflowStep("dedicated[0]", "skipped"),
    )
    assert result.warnings == ("w",)
    apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_lpar_skips_assignments_when_no_lpar_body(monkeypatch):
    hmc = cast(HMCClient, object())
    assignments = LparPcieAssignments(
        dedicated=(DedicatedPcieAssignment("default_profile", "21010010"),)
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.prevalidate_lpar_pcie_assignments",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.create_and_stamp_lpar",
        AsyncMock(return_value=LparCreationResult(True, None, False, ("no body",))),
    )
    apply = AsyncMock()
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.apply_validated_lpar_pcie_assignments",
        apply,
    )

    result = await create_lpar(hmc, "sys1", _creation(), assignments)

    assert result.resource_created is True
    assert result.workflow_completed is False
    assert result.lpar is None
    assert result.steps == (
        WorkflowStep("create", "ok", None),
        WorkflowStep("dedicated[0]", "skipped"),
    )
    assert result.warnings == ("no body",)
    apply.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "error", "ok"])
@pytest.mark.parametrize("stamped", [True, False])
async def test_missing_or_failed_policy_step_stops_followups(
    monkeypatch, status, stamped
):
    from dataclasses import replace

    from hmcpctl.documents.lpar_security_policy import LparSecurityPolicy

    hmc = cast(HMCClient, object())
    creation = replace(_creation(), security_policy=LparSecurityPolicy(secure_boot=0))
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.prevalidate_lpar_pcie_assignments",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.create_and_stamp_lpar",
        AsyncMock(
            return_value=LparCreationResult(
                True,
                {"UUID": "created"},
                stamped,
                (),
                security_policy_step=WorkflowStep("security_policy", status)
                if status
                else None,
            )
        ),
    )
    apply = AsyncMock(return_value=AssignmentResult(True, False, ()))
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.apply_validated_lpar_pcie_assignments", apply
    )
    result = await create_lpar(hmc, "sys1", creation, LparPcieAssignments())
    assert apply.await_count == int(status == "ok" and stamped)
    assert result.workflow_completed == (status == "ok" and stamped)


@pytest.mark.asyncio
async def test_policy_workflow_reports_verification_before_apply(monkeypatch):
    from dataclasses import replace

    from hmcpctl.documents.lpar_security_policy import LparSecurityPolicy

    creation = replace(_creation(), security_policy=LparSecurityPolicy(secure_boot=0))
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.prevalidate_lpar_pcie_assignments",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.create_and_stamp_lpar",
        AsyncMock(
            return_value=LparCreationResult(
                True,
                {"UUID": "created"},
                True,
                (),
                apply_step=WorkflowStep("apply_profile", "ok"),
                security_policy_step=WorkflowStep("security_policy", "ok"),
            )
        ),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.workflows.apply_validated_lpar_pcie_assignments",
        AsyncMock(return_value=AssignmentResult(True, False, ())),
    )
    result = await create_lpar(
        cast(HMCClient, object()), "sys1", creation, LparPcieAssignments()
    )
    assert [step.step for step in result.steps] == [
        "create",
        "security_policy",
        "apply_profile",
    ]
