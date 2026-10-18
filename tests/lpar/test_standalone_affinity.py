"""Standalone PowerOn affinity-assessment contract tests."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, patch

import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.affinity.rest import (
    AffinityAssessmentResult,
    AffinityClassification,
    AffinityEvidence,
    PostActivationAffinityAssessment,
    ProvisionAffinityAssessment,
    assess_post_activation_affinity,
    classify_affinity_outcome,
)
from hmcpctl.operations.lpar.activation import (
    LparPowerResult,
    activation_allows_assessment,
)
from hmcpctl.server_tools.lpar.lifecycle import hmc_power_on_lpar


class _ActivatedClient:
    async def get_quick_property(self, *args: object) -> str:
        return "running"


class _ClientContext:
    async def __aenter__(self) -> object:
        return _ActivatedClient()

    async def __aexit__(self, *args: object) -> None:
        return None


def _request(response: Literal["warn", "fail"] = "warn") -> ProvisionAffinityAssessment:
    return ProvisionAffinityAssessment(
        system_name_or_uuid="system-1",
        lpar_name="lpar-1",
        captured_score=80,
        captured_policy_state="absent",
        captured_minimum=None,
        captured_at=datetime.now(UTC),
        stale_after_seconds=300,
        response=response,
        regression_threshold=5,
        optimization_threshold=5,
    )


def _assessment(
    classification: str, explanation: str = "reason"
) -> PostActivationAffinityAssessment:
    evidence = AffinityEvidence(
        captured_score=80,
        current_score=82,
        predicted_score=84,
        policy_state="absent",
        captured_policy_state="absent",
        configured_minimum=None,
        captured_minimum=None,
        captured_at=datetime.now(UTC).isoformat(),
        assessed_at=datetime.now(UTC).isoformat(),
        stale_after_seconds=300,
        regression_threshold=5,
        optimization_threshold=5,
    )
    assessment = AffinityAssessmentResult(
        classification=cast(AffinityClassification, classification),
        evidence=evidence,
        explanation=explanation,
        recommended_actions=(),
    )
    return PostActivationAffinityAssessment(assessment, 82, 84, False)


def test_unsupported_measurement_is_unavailable_for_warning_intent() -> None:
    outcome = classify_affinity_outcome(_assessment("unsupported-data"), "warn")

    assert outcome.measured is True
    assert outcome.status == "unavailable"
    assert outcome.reason == "reason"


def test_adverse_measurement_warns_for_warning_intent() -> None:
    outcome = classify_affinity_outcome(_assessment("regression"), "warn")

    assert outcome.measured is True
    assert outcome.status == "warned"


def test_adverse_measurement_fails_closed() -> None:
    outcome = classify_affinity_outcome(_assessment("policy-violation"), "fail")

    assert outcome.measured is True
    assert outcome.status == "failed"


def test_unsupported_measurement_fails_closed() -> None:
    outcome = classify_affinity_outcome(_assessment("unsupported-data"), "fail")

    assert outcome.measured is True
    assert outcome.status == "failed"


def test_timeout_does_not_allow_measurement() -> None:
    allowed, reason = activation_allows_assessment(
        LparPowerResult("lpar-1", {"Resource": {"JobID": "job-1", "Status": "RUNNING"}})
    )

    assert allowed is False
    assert "timeout" in reason.lower()


@pytest.mark.asyncio
async def test_measurement_runs_current_prediction_and_policy_reads_after_success() -> (
    None
):
    policy = SimpleNamespace(capability="supported", min_affinity_score=None)
    hmc = cast(HMCClient, SimpleNamespace(config=object()))
    with (
        patch(
            "hmcpctl.operations.affinity.rest.get_lpar_memopt_score",
            new=AsyncMock(return_value={"curr_lpar_score": "82"}),
        ) as current,
        patch(
            "hmcpctl.operations.affinity.rest.plan_lpar_memopt_scores",
            new=AsyncMock(
                return_value=[{"lpar_name": "lpar-1", "predicted_lpar_score": "84"}]
            ),
        ) as predicted,
        patch(
            "hmcpctl.operations.affinity.rest.get_minimum_affinity_policy",
            new=AsyncMock(return_value=policy),
        ) as policy_read,
    ):
        result = await assess_post_activation_affinity(hmc, _request())

    assert result.assessment.classification == "none"
    current.assert_awaited_once()
    predicted.assert_awaited_once()
    policy_read.assert_awaited_once()


@pytest.mark.asyncio
async def test_malformed_measured_score_is_a_validation_failure() -> None:
    policy = SimpleNamespace(capability="supported", min_affinity_score=None)
    hmc = cast(HMCClient, SimpleNamespace(config=object()))
    with (
        patch(
            "hmcpctl.operations.affinity.rest.get_lpar_memopt_score",
            new=AsyncMock(return_value={"curr_lpar_score": "101"}),
        ),
        patch(
            "hmcpctl.operations.affinity.rest.plan_lpar_memopt_scores",
            new=AsyncMock(
                return_value=[{"lpar_name": "lpar-1", "predicted_lpar_score": "84"}]
            ),
        ),
        patch(
            "hmcpctl.operations.affinity.rest.get_minimum_affinity_policy",
            new=AsyncMock(return_value=policy),
        ),
        pytest.raises(ValueError, match="current_score"),
    ):
        await assess_post_activation_affinity(hmc, _request())


def _power_result(status: str) -> LparPowerResult:
    return LparPowerResult("lpar-1", {"Resource": {"JobID": "job-1", "Status": status}})


def test_completed_activation_runs_and_returns_assessment() -> None:
    assessment = AsyncMock(return_value=_assessment("none", "passed reason"))
    with (
        patch(
            "hmcpctl._app.client_from_env",
            return_value=_ClientContext(),
        ),
        patch(
            "hmcpctl.operations.lpar.activation.submit_power_on_lpar",
            new=AsyncMock(return_value=_power_result("COMPLETED_OK")),
        ),
        patch(
            "hmcpctl.operations.lpar.activation.assess_post_activation_affinity",
            new=assessment,
        ),
    ):
        result = hmc_power_on_lpar(
            "lpar-1",
            wait=True,
            system_name_or_uuid="system-1",
            affinity_assessment=_request(),
        )

    assessment.assert_awaited_once()
    assert result.job == _power_result("COMPLETED_OK").job
    assert result.affinity_assessment.status == "passed"
    assert result.affinity_assessment.reason == "passed reason"


@pytest.mark.parametrize("status", ["RUNNING", "COMPLETED_WITH_ERROR"])
def test_unconfirmed_activation_never_runs_assessment(status: str) -> None:
    assessment = AsyncMock()
    with (
        patch(
            "hmcpctl._app.client_from_env",
            return_value=_ClientContext(),
        ),
        patch(
            "hmcpctl.operations.lpar.activation.submit_power_on_lpar",
            new=AsyncMock(return_value=_power_result(status)),
        ),
        patch(
            "hmcpctl.operations.lpar.activation.assess_post_activation_affinity",
            new=assessment,
        ),
    ):
        result = hmc_power_on_lpar(
            "lpar-1",
            wait=True,
            system_name_or_uuid="system-1",
            affinity_assessment=_request(),
        )

    assessment.assert_not_awaited()
    assert result.job == _power_result(status).job
    assert result.affinity_assessment.status == "unavailable"


@pytest.mark.parametrize(
    ("response", "expected"), [("warn", "unavailable"), ("fail", "failed")]
)
def test_malformed_measurement_preserves_job_and_applies_intent(
    response: Literal["warn", "fail"], expected: str
) -> None:
    with (
        patch(
            "hmcpctl._app.client_from_env",
            return_value=_ClientContext(),
        ),
        patch(
            "hmcpctl.operations.lpar.activation.submit_power_on_lpar",
            new=AsyncMock(return_value=_power_result("COMPLETED_OK")),
        ),
        patch(
            "hmcpctl.operations.lpar.activation.assess_post_activation_affinity",
            new=AsyncMock(
                side_effect=ValueError("current_score must be 0 through 100")
            ),
        ),
    ):
        result = hmc_power_on_lpar(
            "lpar-1",
            wait=True,
            system_name_or_uuid="system-1",
            affinity_assessment=_request(response),
        )

    assert result.job == _power_result("COMPLETED_OK").job
    assert result.affinity_assessment.status == expected
    assert result.affinity_assessment.measured is False
    assert "current_score" in result.affinity_assessment.reason
