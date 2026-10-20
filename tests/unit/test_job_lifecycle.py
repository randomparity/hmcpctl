"""Tests for shared submitted-job lifecycle handling."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from hmcpctl.jobs import (
    job_identifier,
    job_outcome,
    validate_wait_timing,
    vios_stdout,
)
from hmcpctl.operations.updates.models import (
    PlatformUpdateParameter,
    VIOSUpdateSource,
    VIOSUpgradeSource,
)

_SUCCESSFUL_TERMINAL_STATUSES = {"COMPLETED", "COMPLETED_OK"}
_ACTIONABLE_TERMINAL_STATUSES = {
    "CANCELED_BEFORE_START",
    "CANCELED_WHILE_RUNNING",
    "COMPLETED_WITH_ERROR",
    "COMPLETED_WITH_WARNINGS",
    "FAILED_BEFORE_COMPLETION",
    "FAILED_BEFORE_COMPLETION_RETRY",
    "FAILED_TO_START",
}


def test_platform_update_builds_a_pydantic_schema() -> None:
    schema = PlatformUpdateParameter.model_json_schema()

    assert set(schema["properties"]) == {
        "SystemFirmwareUpdate",
        "VIOSUpdate",
    }


@pytest.mark.parametrize("source", [VIOSUpdateSource, VIOSUpgradeSource])
def test_vios_source_builds_a_pydantic_type_adapter(source) -> None:
    schema = TypeAdapter(source).json_schema()

    variants = [
        schema["$defs"][entry["$ref"].rsplit("/", 1)[1]] for entry in schema["anyOf"]
    ]
    assert all("ResourceType" in variant["properties"] for variant in variants)
    assert all("ResourceType" in variant["required"] for variant in variants)


def test_vios_source_properties_are_operation_specific() -> None:
    update_schema = TypeAdapter(VIOSUpdateSource).json_schema()
    upgrade_schema = TypeAdapter(VIOSUpgradeSource).json_schema()
    update = [
        update_schema["$defs"][entry["$ref"].rsplit("/", 1)[1]]["properties"]
        for entry in update_schema["anyOf"]
    ]
    upgrade = [
        upgrade_schema["$defs"][entry["$ref"].rsplit("/", 1)[1]]["properties"]
        for entry in upgrade_schema["anyOf"]
    ]

    assert all(
        "RestartVIOS" in properties and "Disks" not in properties
        for properties in update
    )
    assert all(
        "Disks" in properties and "RestartVIOS" not in properties
        for properties in upgrade
    )


@pytest.mark.parametrize(
    ("parameters", "expected"),
    [
        ({"ParameterName": "stdOut", "ParameterValue": " log "}, "log"),
        (
            [
                None,
                {"ParameterName": "stdout", "ParameterValue": "wrong case"},
                {"ParameterName": "stdOut", "ParameterValue": 7},
                {"ParameterName": "stdOut", "ParameterValue": "  first  "},
                {"ParameterName": "stdOut", "ParameterValue": "second"},
            ],
            "first",
        ),
        ({"ParameterName": "stdOut", "ParameterValue": "   "}, None),
        ("malformed", None),
    ],
)
def test_vios_stdout_extracts_first_nonempty_string(parameters, expected) -> None:
    job = {"Resource": {"Results": {"JobParameter": parameters}}}

    assert vios_stdout(job) == expected


@pytest.mark.parametrize(
    "job",
    [None, {}, {"Resource": "bad"}, {"Resource": {"Results": "bad"}}],
)
def test_vios_stdout_ignores_malformed_job_shapes(job) -> None:
    assert vios_stdout(job) is None


_READ_UUID = "65680cb7-0000-4000-8000-000000000002"


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        ({"UUID": "top"}, "top"),
        ({"Resource": {"JobID": "nested"}}, "nested"),
        ({"UUID": "entry-uuid", "Resource": {"JobID": "nested"}}, "nested"),
        ({"link": "https://hmc.test/rest/api/uom/jobs/from-link"}, "from-link"),
        (
            {"link": f"https://hmc.test/rest/api/uom/jobs/1787837921263/{_READ_UUID}"},
            "1787837921263",
        ),
        ({"UUID": "  trimmed  "}, "trimmed"),
        ({"UUID": 42, "Resource": {"JobID": "nested-id"}}, "nested-id"),
        ({"UUID": "   ", "Resource": {"JobID": "nested-id"}}, "nested-id"),
        ({"Resource": {"JobID": ""}}, None),
        ({}, None),
    ],
)
def test_job_identifier_accepts_only_nonempty_strings(job, expected) -> None:
    assert job_identifier(job) == expected


def test_job_identifier_hands_out_the_job_id_of_a_real_job_entry() -> None:
    """The envelope a V10R3 HMC returns: entry UUID, JobID and per-read link all differ.

    Only the JobID resolves through the global jobs path there; the entry UUID is
    answered with HTTP 406 (issue #1160).
    """
    job = {
        "UUID": "93f544bb-0000-4000-8000-000000000001",
        "title": "JobResponse",
        "link": f"https://hmc.test/rest/api/uom/jobs/1787837921263/{_READ_UUID}",
        "ResourceType": "JobResponse",
        "Resource": {"JobID": "1787837921263", "Status": "COMPLETED_OK"},
    }

    assert job_identifier(job) == "1787837921263"
    outcome = job_outcome("1787837921263", job)
    assert (outcome.job_id, outcome.job_href) == (
        "1787837921263",
        "https://hmc.test/rest/api/uom/jobs/1787837921263",
    )


@pytest.mark.parametrize(
    "link",
    [
        "https://hmc.test/rest/api/uom/jobs/1787837921263",
        "https://hmc.test/rest/api/uom/jobs/1787837921263/not-a-uuid",
    ],
)
def test_job_outcome_echoes_any_other_link_verbatim(link) -> None:
    job = {"Resource": {"JobID": "1787837921263"}, "link": link}

    assert job_outcome("1787837921263", job).job_href == link


@pytest.mark.parametrize("link", ["nulljobs/1787837921263", "jobs/1787837921263"])
def test_job_outcome_hands_out_no_relative_link(link) -> None:
    """A relative link would be refused by the client when passed back as job_href."""
    job = {"Resource": {"JobID": "1787837921263"}, "link": link}

    assert job_outcome("1787837921263", job).job_href is None


def test_job_identifier_skips_truthy_non_mapping_resource() -> None:
    job = {"Resource": "unexpected", "link": "/rest/api/uom/jobs/link-id"}

    assert job_identifier(job) == "link-id"


def test_job_outcome_normalizes_response_identity_and_result_error() -> None:
    job = {
        "Resource": {
            "JobID": " normalized-id ",
            "Status": "COMPLETED_WITH_ERROR",
            "Results": {
                "JobParameter": [
                    {"ParameterName": "returnCode", "ParameterValue": "1"},
                    {"ParameterName": "result", "ParameterValue": " failed "},
                ]
            },
        }
    }

    outcome = job_outcome("requested-id", job)

    assert outcome.job_id == "normalized-id"
    assert outcome.status == "COMPLETED_WITH_ERROR"
    assert outcome.timed_out is False
    assert outcome.error == "failed"
    assert outcome.job is job


def test_job_outcome_falls_back_to_requested_identity_and_status() -> None:
    job = {"Resource": {"Status": "FAILED_BEFORE_COMPLETION"}}

    outcome = job_outcome(" requested-id ", job)

    assert outcome.job_id == "requested-id"
    assert outcome.status == "FAILED_BEFORE_COMPLETION"
    assert outcome.timed_out is False
    assert outcome.error == "Job ended with status FAILED_BEFORE_COMPLETION"


def test_job_outcome_ignores_an_undocumented_exception_element() -> None:
    """No capture or reference shows a `ResponseException`; job text is in Results."""
    job = {
        "Resource": {
            "Status": "COMPLETED_WITH_ERROR",
            "ResponseException": {"Message": "exception text"},
        }
    }

    assert job_outcome("job-id", job).error == (
        "Job ended with status COMPLETED_WITH_ERROR"
    )


@pytest.mark.parametrize(
    "names",
    [
        ("result", "ErrorData"),
        ("ErrorData", "result"),
        ("result", "errorData"),
        ("errorData", "result"),
    ],
)
def test_job_outcome_prefers_error_data_regardless_of_parameter_order(names) -> None:
    values = {
        "result": "ordinary output",
        "ErrorData": "failure text",
        "errorData": "failure text",
    }
    job = {
        "Resource": {
            "Status": "COMPLETED_WITH_ERROR",
            "Results": {
                "JobParameter": [
                    {"ParameterName": name, "ParameterValue": values[name]}
                    for name in names
                ]
            },
        }
    }

    assert job_outcome("job-id", job).error == "failure text"


def test_job_outcome_surfaces_detailed_status_when_error_data_is_absent() -> None:
    job = {
        "Resource": {
            "Status": "COMPLETED_WITH_ERROR",
            "Results": {
                "JobParameter": {
                    "ParameterName": "detailedStatus",
                    "ParameterValue": "target system unavailable",
                }
            },
        }
    }

    assert job_outcome("job-id", job).error == "target system unavailable"


def test_job_outcome_tolerates_truthy_non_mapping_resource() -> None:
    outcome = job_outcome(" requested-id ", {"Resource": "unexpected"})

    assert outcome.job_id == "requested-id"
    assert outcome.status is None
    assert outcome.timed_out is True
    assert outcome.error is None


def test_job_outcome_does_not_report_success_result_as_error() -> None:
    job = {
        "Resource": {
            "JobID": "job-id",
            "Status": "COMPLETED_OK",
            "Results": {
                "JobParameter": {
                    "ParameterName": "result",
                    "ParameterValue": "success details",
                }
            },
        }
    }

    assert job_outcome("job-id", job).error is None


def test_job_outcome_marks_missing_entry_as_timed_out() -> None:
    outcome = job_outcome("job-id", None)

    assert outcome.job_id == "job-id"
    assert outcome.status is None
    assert outcome.timed_out is True
    assert outcome.error is None
    assert outcome.job is None


@pytest.mark.parametrize(
    ("timeout_seconds", "poll_interval", "message"),
    [
        (-1, 5, "timeout_seconds"),
        (300, -1, "poll_interval"),
        (300, 0, "poll_interval"),
    ],
)
def test_validate_wait_timing_rejects_invalid_values(
    timeout_seconds, poll_interval, message
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_wait_timing(True, timeout_seconds, poll_interval)


def test_validate_wait_timing_ignores_unused_values() -> None:
    validate_wait_timing(False, -1, -1)


@pytest.mark.parametrize(
    "status", ["FAILED_BEFORE_COMPLETION", "COMPLETED_WITH_ERROR", "FAILED_TO_START"]
)
@pytest.mark.parametrize(
    "shape", ["attributes", "mapping", "list", "scalar", "missing"]
)
def test_job_outcome_native_diagnostic_shapes_preserve_failure(status, shape):
    from hmcpctl.xmlutil import parse_feed

    job = parse_feed(
        '<entry xmlns="http://www.w3.org/2005/Atom"><id>urn:uuid:job-1</id>'
        f"<content><JobResponse><JobID>job-1</JobID><Status>{status}</Status>"
        '<Results><JobParameter><ParameterName ksv="V1_0">ErrorData</ParameterName>'
        '<ParameterValue ksv="V1_0">native failure</ParameterValue>'
        "</JobParameter></Results></JobResponse></content></entry>"
    )[0]
    parameter = job["Resource"]["Results"]["JobParameter"]
    if shape != "attributes":
        parameter["ParameterName"] = {
            "mapping": {"unexpected": "value"},
            "list": ["ErrorData"],
            "scalar": 1,
            "missing": None,
        }[shape]
    outcome = job_outcome("job-1", job)
    assert outcome.status == status and outcome.timed_out is False
    assert outcome.job_id == "job-1" and outcome.job is job
    assert outcome.error == (
        "native failure" if shape == "attributes" else f"Job ended with status {status}"
    )
