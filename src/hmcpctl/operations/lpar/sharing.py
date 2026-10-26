"""Independent, bounded evidence for one sharing configuration submission."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET  # nosec B405 - client supplies defused XML
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import (
    LparResources,
    has_sharing_change,
    partition_updates,
    sharing_observation,
    validate_resource_inputs,
    validate_sharing_update,
)
from hmcpctl.errors import MAX_ERROR_BODY_BYTES, HMCError, LparUpdateError
from hmcpctl.xmlutil import element_to_dict

from .profile_sync import change_location_of, resource_with_change_location

_PPC = "PartitionProcessorConfiguration"
_EXACT_PATHS = {f"{_PPC}/SharingMode", f"{_PPC}/HasDedicatedProcessors"}
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


async def prevalidate_sharing_change(
    hmc: HMCClient, uuid: str, resources: LparResources
) -> None:
    """Exercise the common mapper and actual XML editor before earlier writes."""
    validate_resource_inputs(resources)

    def updates(lpar: ET.Element) -> dict[str, str]:
        mapping = partition_updates(lpar, resources=resources)
        if has_sharing_change(resources):
            _observation(lpar, uuid, tuple(mapping))
        return mapping

    await hmc.validate_logical_partition_update(
        uuid, updates, "the partition resources"
    )


def _observation(lpar: ET.Element, uuid: str, paths: tuple[str, ...]) -> dict[str, Any]:
    observed = sharing_observation(lpar, paths)
    if UUID(observed["uuid"]) != UUID(uuid):
        raise ValueError(
            "Independent LogicalPartition UUID does not match selected UUID"
        )
    return observed


def _number(value: str | None) -> Decimal | None:
    if value is None or not _NUMBER.fullmatch(value):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _equivalent(path: str, actual: str | None, expected: str | None) -> bool:
    if path in _EXACT_PATHS:
        return actual == expected
    left, right = _number(actual), _number(expected)
    return left is not None and right is not None and left == right


def _branch_checks(
    branch: dict[str, Any] | None, dedicated: bool, mode: str, weight: int | None
) -> list[bool | None]:
    if branch is None:
        return [None]
    checks: list[bool | None] = [
        branch["dedicated"] == dedicated,
        branch["mode"] == mode,
    ]
    if not dedicated and mode == "uncapped":
        checks.append(
            branch["weight"] == weight
            if branch["weight_status"] == "valid" and weight is not None
            else None
        )
    return checks


def _verification(checks: list[bool | None]) -> str:
    if False in checks:
        return "mismatched"
    return "unavailable" if None in checks else "matched"


def _verify(
    after: dict[str, Any], expected: dict[str, Any], dedicated: bool
) -> tuple[str, str]:
    checks: list[bool | None] = []
    for path, target in expected["configured"].items():
        actual = after["requested"][path]
        if actual is None or (path not in _EXACT_PATHS and _number(actual) is None):
            checks.append(None)
        else:
            checks.append(_equivalent(path, actual, target))
    configured = after["configured"]
    weight = expected["weight"]
    if (
        weight is None
        and configured is not None
        and configured["weight_status"] == "valid"
    ):
        weight = configured["weight"]
    mode = expected["effective_mode"]
    for branch in (configured, after["current"]):
        checks.extend(_branch_checks(branch, dedicated, mode, weight))
    return _verification(checks), _verification(
        _branch_checks(after["runtime"], dedicated, mode, weight)
    )


def _configuration_comparison(before: dict[str, Any], after: dict[str, Any]) -> str:
    checks: list[bool | None] = []
    for path, value in after["requested"].items():
        # Raw context or weight text cannot establish change without a valid branch.
        if path in _EXACT_PATHS or path == (
            f"{_PPC}/SharedProcessorConfiguration/UncappedWeight"
        ):
            continue
        previous = before["requested"][path]
        checks.append(
            _equivalent(path, value, previous)
            if _number(value) is not None and _number(previous) is not None
            else None
        )
    for name in ("configured", "current"):
        previous, observed = before[name], after[name]
        if previous is None:
            checks.append(None)
        else:
            checks.extend(
                _branch_checks(
                    observed,
                    previous["dedicated"],
                    previous["mode"],
                    previous["weight"],
                )
            )
    return _verification(checks)


def _bounded(value: Any, warnings: list[str]) -> Any:
    """Render diagnostic copies only after all comparisons use full evidence."""
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) <= MAX_ERROR_BODY_BYTES:
            return value
        warning = "Sharing diagnostic strings were truncated to the byte limit."
        if warning not in warnings:
            warnings.append(warning)
        marker = "...[truncated]"
        return (
            encoded[: MAX_ERROR_BODY_BYTES - len(marker)].decode(
                "utf-8", errors="ignore"
            )
            + marker
        )
    if isinstance(value, dict):
        return {key: _bounded(item, warnings) for key, item in value.items()}
    return value


async def apply_sharing_change(
    hmc: HMCClient, uuid: str, resources: LparResources, subject: str
) -> dict[str, Any]:
    """Submit once and verify configured and current state independently of its echo.

    The caller owns containment, ownership and any workflow preflight. Ordinary
    pre-submit refusals propagate. Only typed post-attempt errors become outcomes.
    """
    validate_resource_inputs(resources)
    before: dict[str, Any] = {}
    expected: dict[str, Any] = {}
    dedicated = False
    paths: tuple[str, ...] = ()

    def updates(lpar: ET.Element) -> dict[str, str]:
        nonlocal before, expected, dedicated, paths
        dedicated, mode = validate_sharing_update(lpar, resources)
        mapping = partition_updates(lpar, resources=resources)
        paths = tuple(mapping)
        before = _observation(lpar, uuid, paths)
        expected = {
            "configured": mapping,
            "effective_mode": mode,
            "weight": resources.uncapped_weight,
        }
        return mapping

    submission, submission_error = "acknowledged", None
    try:
        await hmc.update_logical_partition(uuid, updates, subject)
    except LparUpdateError as exc:
        submission, submission_error = exc.submission, str(exc)
    after, read_error, resource = None, None, {}
    configuration, runtime = "unavailable", "unavailable"
    try:
        document, _ = await hmc.read_logical_partition_document(uuid)
        after = _observation(document, uuid, paths)
        configuration, runtime = _verify(after, expected, dedicated)
        resource = element_to_dict(document)
    except (HMCError, ValueError) as exc:
        read_error = str(exc)
    comparison = (
        _configuration_comparison(before, after) if after is not None else "unavailable"
    )
    outcome = "unverified"
    if configuration == "matched":
        outcome = "applied"
    elif comparison == "mismatched":
        outcome = "partial"
    elif comparison == "matched" and submission == "rejected":
        outcome = "rejected"
    entry = {"UUID": uuid, "Resource": resource}
    location = change_location_of(entry)
    warnings = []
    if location.lives_in != "current-configuration-and-profile":
        warnings.append(location.summary())
    if after is None:
        warnings.append(
            "Independent partition read or identity unavailable; current Resource is unavailable."
        )
    if outcome == "applied" and submission != "acknowledged":
        warnings.append(
            "Matching configuration after an uncertain or rejected submission does not establish causality."
        )
    if runtime != "matched":
        warnings.append(
            "Runtime verification is "
            + runtime
            + "; no guest execution proof is established."
        )
    if outcome != "applied":
        warnings.append(
            "Sharing configuration completion is not verified; later workflow steps must be skipped."
        )
    result = resource_with_change_location(entry, location)
    result.update(
        completion_scope="current-configuration",
        outcome=outcome,
        workflow_completed=configuration == "matched",
        submission_status=submission,
        configuration_verification=configuration,
        runtime_verification=runtime,
        before=_bounded(before, warnings),
        after=_bounded(after, warnings),
        expected=expected,
        submission_error=_bounded(submission_error, warnings),
        read_error=_bounded(read_error, warnings),
        warnings=warnings,
    )
    result["warnings"] = [_bounded(warning, warnings) for warning in warnings]
    return result
