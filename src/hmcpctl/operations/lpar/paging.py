"""Authorize one paging-provider switch and independently reduce its evidence."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ...errors import HMCError
from ...resource_identity import is_uuid, resolve_system_uuid
from ...ssh import memory as native
from ...ssh.transport import HMCCLIError
from ...xmlutil import leaf_text
from .creation_memory import PagingProvider, _detail, _identity, _inventory
from .ownership import authorize_lpar_mutation
from .paging_observation import PagingObservation, PartitionIdentity, observe_paging

if TYPE_CHECKING:
    from ...client.core import HMCClient


@dataclass(frozen=True)
class PagingSwitchoverResult:
    system_uuid: str
    target: PartitionIdentity
    expected_current: PagingProvider
    expected_other: PagingProvider
    submission: Literal["acknowledged", "remote-error", "unknown"]
    observation: Literal["expected", "unchanged", "other", "unavailable"]
    preservation: Literal["verified", "drifted", "unavailable"]
    outcome: Literal["completed", "partial", "rejected", "unverified"]
    workflow_completed: bool
    before: PagingObservation
    after: PagingObservation | None
    remote_refusal_code: str | None
    diagnostics: tuple[str, ...]


def compare_paging(
    before: PagingObservation, after: PagingObservation
) -> tuple[Literal["verified", "drifted", "unavailable"], tuple[str, ...]]:
    """A readable contradiction wins over absence, retaining both categories."""
    drift = list(after.invalid)
    missing = list(after.unavailable)
    for field in (
        "system_uuid",
        "system_name",
        "target",
        "pool_uuid",
        "pool_id",
        "state",
        "shared",
        "uom_redundant",
        "native_redundant",
    ):
        value = getattr(after, field)
        if value is None:
            missing.append(field)
        elif value != getattr(before, field):
            drift.append(field)
    for field in ("providers", "paths"):
        value = getattr(after, field)
        if len(value) != 2:
            missing.append(field)
        elif set(value) != set(getattr(before, field)):
            drift.append(field)
    if (
        len(after.providers) != 2
        or len(before.providers) != 2
        or len(before.provider_dedicated) != 2
        or len(after.provider_dedicated) != 2
        or any(v is None for v in after.provider_dedicated)
    ):
        missing.append("provider_dedicated")
    elif dict(zip(after.providers, after.provider_dedicated, strict=True)) != dict(
        zip(before.providers, before.provider_dedicated, strict=True)
    ):
        drift.append("provider_dedicated")
    if before.unique_device_id is not None:
        if after.unique_device_id is None:
            missing.append("unique_device_id")
        elif before.unique_device_id != after.unique_device_id:
            drift.append("unique_device_id")
    if after.configured is None or before.configured is None:
        missing.append("configured")
    else:
        fields = ("primary", "secondary")
        if after.configured.plane == before.configured.plane:
            fields += ("profile_uuid", "profile_name", "setting_id")
        for field in fields:
            value = getattr(after.configured, field)
            if value != getattr(before.configured, field):
                (missing if value is None else drift).append("configured." + field)
        if after.configured.plane != before.configured.plane:
            missing.append("configured.plane")
    for current in (after.current_uom, after.current_native):
        if current is None:
            missing.append("current-provider")
        elif current not in before.providers:
            drift.append("current-outside-pair")
    if (
        after.current_uom
        and after.current_native
        and after.current_uom != after.current_native
    ):
        drift.append("current-readers-disagree")
    diagnostics = tuple(dict.fromkeys(drift + missing))
    return (
        "drifted" if drift else "unavailable" if missing else "verified"
    ), diagnostics


def reduce_paging_attempt(
    system_uuid: str,
    before: PagingObservation,
    after: PagingObservation | None,
    expected_current: PagingProvider,
    expected_other: PagingProvider,
    submission: Literal["acknowledged", "remote-error", "unknown"],
    remote_refusal_code: str | None,
    diagnostics: tuple[str, ...],
) -> PagingSwitchoverResult:
    """Effective-state proof is independent of command completion and causation."""
    preservation, reasons = (
        compare_paging(before, after)
        if after
        else ("unavailable", ("after-observation",))
    )
    observation: Literal["expected", "unchanged", "other", "unavailable"] = (
        "unavailable"
    )
    if after and after.current_uom and after.current_native:
        if after.current_uom == after.current_native == expected_other:
            observation = "expected"
        elif after.current_uom == after.current_native == expected_current:
            observation = "unchanged"
        else:
            observation = "other"
    outcome: Literal["completed", "partial", "rejected", "unverified"] = "unverified"
    if observation == "expected" and preservation == "verified":
        outcome = "completed"
    elif preservation == "drifted":
        outcome = "partial"
    elif (
        preservation == "verified"
        and observation == "unchanged"
        and submission == "remote-error"
    ):
        outcome = "rejected"
    return PagingSwitchoverResult(
        system_uuid,
        before.target,
        expected_current,
        expected_other,
        submission,
        observation,
        preservation,
        outcome,
        outcome == "completed",
        before,
        after,
        remote_refusal_code,
        tuple(dict.fromkeys(diagnostics + reasons)),
    )


def _require_complete(observation: PagingObservation) -> None:
    if compare_paging(observation, observation)[0] != "verified":
        raise ValueError(
            "Paging prerequisites are unavailable or inconsistent; inspect current configuration before a fresh attempt"
        )


async def _authorize(hmc: HMCClient, observation: PagingObservation) -> None:
    for name in (observation.target.name, *(p.name for p in observation.providers)):
        if not math.isfinite(hmc.config.ssh_timeout) or hmc.config.ssh_timeout <= 0:
            raise ValueError("Paging switch requires a finite positive ssh_timeout")
        try:
            await authorize_lpar_mutation(
                hmc,
                observation.system_name,
                name,
                ownership_override=False,
                max_output_bytes=5 * 1024 * 1024,
            )
        except PermissionError:
            raise PermissionError(
                "Paging switchover has no ownership override; resolve ownership before a fresh authorized attempt"
            ) from None
        except HMCCLIError:
            raise HMCCLIError(
                "Paging ownership precheck requires readable ownership metadata before fresh authorization"
            ) from None


async def _resolve_target(
    hmc: HMCClient, system_name_or_uuid: str, lpar_name_or_uuid: str
) -> tuple[str, str, PartitionIdentity]:
    system = (await resolve_system_uuid(hmc, system_name_or_uuid)).lower()
    inventory = await _inventory(hmc, system)
    if is_uuid(lpar_name_or_uuid):
        matches = [e for e in inventory if e["UUID"] == lpar_name_or_uuid.lower()]
    else:
        matches = [
            e
            for e in inventory
            if _identity(e, e["ResourceType"]).name == lpar_name_or_uuid
        ]
    if len(matches) != 1:
        raise ValueError("Paging consumer must resolve uniquely in the selected system")
    entry = matches[0]
    identity = _identity(entry, entry["ResourceType"])
    target = PartitionIdentity(
        identity.uuid, identity.name, identity.partition_id, entry["ResourceType"]
    )
    system_entry = await _detail(hmc, "ManagedSystem", system)
    system_name = leaf_text(system_entry["Resource"].get("SystemName"))
    if not isinstance(system_name, str) or not system_name.strip():
        raise ValueError("Paging switch requires the canonical system name")
    return system, system_name, target


async def switch_lpar_paging_vios(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    *,
    system_name_or_uuid: str,
    expected_current_paging_vios_name_or_uuid: str,
    exclusive_writer_window: bool,
) -> PagingSwitchoverResult:
    """One coordinated native toggle, never retried or automatically reversed."""
    if type(exclusive_writer_window) is not bool or exclusive_writer_window is not True:
        raise ValueError("Paging switch requires explicit exclusive_writer_window=true")
    if any(
        not isinstance(v, str) or not v.strip()
        for v in (
            lpar_name_or_uuid,
            system_name_or_uuid,
            expected_current_paging_vios_name_or_uuid,
        )
    ):
        raise ValueError(
            "Paging switch requires nonblank system, partition and expected provider"
        )
    if not math.isfinite(hmc.config.ssh_timeout) or hmc.config.ssh_timeout <= 0:
        raise ValueError("Paging switch requires a finite positive ssh_timeout")
    system, system_name, target = await _resolve_target(
        hmc, system_name_or_uuid, lpar_name_or_uuid
    )
    before = await observe_paging(hmc, system, system_name, target)
    _require_complete(before)
    await _authorize(hmc, before)
    expected = [
        p
        for p in before.providers
        if (
            p.uuid == expected_current_paging_vios_name_or_uuid.lower()
            if is_uuid(expected_current_paging_vios_name_or_uuid)
            else p.name == expected_current_paging_vios_name_or_uuid
        )
    ]
    if (
        len(expected) != 1
        or before.current_uom != before.current_native
        or before.current_uom != expected[0]
    ):
        raise ValueError(
            "Paging expected current provider is stale; inspect before a fresh attempt"
        )
    current = expected[0]
    other = next(p for p in before.providers if p != current)
    fresh = await observe_paging(hmc, system, system_name, target)
    _require_complete(fresh)
    if (
        compare_paging(before, fresh)[0] != "verified"
        or fresh.current_uom != current
        or fresh.current_native != current
    ):
        raise ValueError("Paging facts changed before submission; nothing was sent")
    await _authorize(hmc, fresh)
    submission: Literal["acknowledged", "remote-error", "unknown"] = "acknowledged"
    code = None
    diagnostics = ()
    try:
        await native.switch_lpar_paging_provider(
            hmc.config, system_name, target.partition_id
        )
    except HMCError as error:
        submission = (
            "remote-error"
            if getattr(error, "remote_nonzero_completed", False) is True
            else "unknown"
        )
        candidate = getattr(error, "remote_refusal_code", None)
        code = (
            candidate
            if isinstance(candidate, str)
            and re.fullmatch(r"HSCL[A-Z0-9]{4}", candidate)
            else None
        )
        diagnostics = (
            "native-remote-error"
            if submission == "remote-error"
            else "native-completion-unknown",
        )
    try:
        after = await observe_paging(hmc, system, system_name, target)
    except (HMCError, ValueError):
        after = None
    return reduce_paging_attempt(
        system, before, after, current, other, submission, code, diagnostics
    )
