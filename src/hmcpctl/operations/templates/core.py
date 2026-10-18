"""Shared partition-template deployment workflow."""

from __future__ import annotations

import logging
from typing import Any

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.ownership import stamp_created_lpar_ownership

from ...errors import HMCError
from ...jobs import (
    DEFAULT_JOB_POLL_INTERVAL,
    DEFAULT_JOB_TIMEOUT_SECONDS,
    SUCCESSFUL_JOB_STATUSES,
    validate_wait_timing,
    wait_for_submitted_job,
)
from ...resource_identity import resolve_system_uuid
from ..error_translation import translate_template_error

_logger = logging.getLogger(__name__)

MANUAL_TEMPLATE_STAMP_WARNING = (
    "ownership stamp not attempted: template deployment does not identify and stamp "
    "the new LPAR; list partitions to identify it, then set its description"
)
BASELINE_SNAPSHOT_WARNING = (
    "ownership stamp not attempted: baseline LPAR snapshot is unavailable"
)
POST_SNAPSHOT_WARNING = (
    "ownership stamp not attempted: post-deployment LPAR snapshot is unavailable"
)


def _snapshot_uuids(entries: list[dict[str, Any]]) -> set[str] | None:
    uuids: set[str] = set()
    for entry in entries:
        uuid = entry.get("UUID")
        if not isinstance(uuid, str) or not uuid.strip():
            return None
        uuids.add(uuid)
    return uuids


def _new_lpar_from_snapshots(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    before_uuids = _snapshot_uuids(before)
    after_uuids = _snapshot_uuids(after)
    if before_uuids is None or after_uuids is None:
        return None, (
            "ownership stamp not attempted: an LPAR snapshot is missing a usable UUID"
        )

    new_uuids = after_uuids - before_uuids
    if not new_uuids:
        return None, "ownership stamp not attempted: deployment produced no new LPAR"
    if len(new_uuids) != 1:
        return None, (
            "ownership stamp not attempted: deployment interval contains "
            f"{len(new_uuids)} new LPARs"
        )

    new_uuid = next(iter(new_uuids))
    matches = [entry for entry in after if entry["UUID"] == new_uuid]
    if len(matches) != 1:
        return None, (
            "ownership stamp not attempted: post-deployment LPAR snapshot contains "
            "duplicate UUIDs"
        )
    return matches[0], None


async def list_partition_templates(hmc: HMCClient) -> list[dict[str, Any]]:
    """List templates with domain-specific HMC error translation."""
    try:
        return await hmc.list_partition_templates()
    except HMCError as exc:
        translated = translate_template_error(exc)
        if translated is exc:
            raise
        raise translated from exc


async def get_partition_template(
    hmc: HMCClient, template_uuid: str
) -> dict[str, Any] | None:
    """Fetch one template with domain-specific HMC error translation."""
    try:
        return await hmc.get_partition_template(template_uuid)
    except HMCError as exc:
        translated = translate_template_error(exc)
        if translated is exc:
            raise
        raise translated from exc


async def deploy_partition_template(
    hmc: HMCClient,
    draft_template_uuid: str,
    target_system_name_or_uuid: str,
    *,
    wait: bool = False,
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_interval: int = DEFAULT_JOB_POLL_INTERVAL,
) -> dict[str, Any]:
    """Submit a template deployment and optionally wait for its terminal job."""
    validate_wait_timing(wait, timeout_seconds, poll_interval)
    target_system_uuid = await resolve_system_uuid(hmc, target_system_name_or_uuid)
    baseline: list[dict[str, Any]] | None = None
    if wait:
        try:
            baseline = await hmc.list_logical_partitions(target_system_uuid)
        except HMCError as exc:
            _logger.warning(
                "Cannot capture template-deployment LPAR baseline for system %s "
                "(HTTP status %s, exception %s, cause %s)",
                target_system_uuid,
                exc.status_code,
                type(exc).__name__,
                type(exc.__cause__).__name__ if exc.__cause__ is not None else None,
            )
    try:
        submitted_job = await hmc.deploy_partition_template(
            draft_template_uuid, target_system_uuid
        )
    except HMCError as exc:
        translated = translate_template_error(exc)
        if translated is exc:
            raise
        raise translated from exc
    selected_job = await wait_for_submitted_job(
        hmc, submitted_job, wait, timeout_seconds, poll_interval
    )
    if not wait:
        return {
            "job": selected_job,
            "ownership_stamped": None,
            "warnings": [MANUAL_TEMPLATE_STAMP_WARNING],
        }

    status = ((selected_job or {}).get("Resource") or {}).get("Status")
    if status not in SUCCESSFUL_JOB_STATUSES:
        return {
            "job": selected_job,
            "ownership_stamped": None,
            "warnings": [
                (
                    "ownership stamp not attempted: template deployment job returned "
                    f"status {status!r}"
                )
            ],
        }
    if baseline is None:
        return {
            "job": selected_job,
            "ownership_stamped": None,
            "warnings": [BASELINE_SNAPSHOT_WARNING],
        }

    try:
        after = await hmc.list_logical_partitions(target_system_uuid)
    except HMCError as exc:
        _logger.warning(
            "Cannot capture post-deployment LPAR snapshot for system %s "
            "(HTTP status %s, exception %s, cause %s)",
            target_system_uuid,
            exc.status_code,
            type(exc).__name__,
            type(exc.__cause__).__name__ if exc.__cause__ is not None else None,
        )
        return {
            "job": selected_job,
            "ownership_stamped": None,
            "warnings": [POST_SNAPSHOT_WARNING],
        }

    created_lpar, inference_warning = _new_lpar_from_snapshots(baseline, after)
    if created_lpar is None:
        if inference_warning is None:
            raise ValueError("Template deployment could not infer the created LPAR")
        return {
            "job": selected_job,
            "ownership_stamped": None,
            "warnings": [inference_warning],
        }

    ownership_stamped, warnings = await stamp_created_lpar_ownership(
        hmc,
        target_system_uuid,
        target_system_name_or_uuid,
        created_lpar,
    )
    return {
        "job": selected_job,
        "ownership_stamped": ownership_stamped,
        "warnings": warnings,
    }
