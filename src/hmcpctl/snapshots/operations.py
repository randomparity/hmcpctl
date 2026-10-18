"""Presentation-neutral portable LPAR snapshot capture."""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal, overload

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.affinity.rest import (
    AffinityAssessmentInput,
    AffinityAssessmentResult,
    PolicyState,
    assess_affinity,
)
from hmcpctl.operations.affinity.ssh import (
    get_lpar_memopt_score,
    get_minimum_affinity_policy,
    get_system_memopt_score,
    list_resource_group_memopt_scores,
    plan_lpar_memopt_scores,
    plan_resource_group_memopt_scores,
    plan_system_memopt_score,
)
from hmcpctl.operations.systems.fleet import operating_system_entry
from hmcpctl.resource_identity import (
    resolve_lpar_uuid,
    resolve_system_name,
    resolve_system_uuid,
)
from hmcpctl.ssh.profiles import read_lpar_profile_record
from hmcpctl.xmlutil import console_version, leaf_text, mtms_parts

from .models import (
    MINIMUM_AFFINITY_POLICY_MEDIA_TYPE,
    PLACEMENT_MEDIA_TYPE,
    PROFILE_MEDIA_TYPE,
    SCORES_MEDIA_TYPE,
    HMCIdentity,
    LparIdentity,
    LparSnapshot,
    NativeProfile,
    NormalizedConfiguration,
    ObservationEnvelope,
    SnapshotCapability,
    SnapshotConfiguration,
    SnapshotInspection,
    SnapshotObservations,
    SnapshotSource,
    SystemIdentity,
    _normalized_from_profile,
    _parse_profile,
    inspect_snapshot,
    parse_snapshot,
    serialize_snapshot,
)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _captured_lpar_score(snapshot: LparSnapshot) -> int | None:
    """Select the captured LPAR score that matches the snapshot identity."""
    scores = snapshot.observations.scores
    if scores is None:
        return None
    current = scores.data.get("current")
    if not isinstance(current, dict):
        return None
    matches = _matching_lpar_score_rows(current.get("lpar"), snapshot.source.lpar)
    if len(matches) != 1:
        return None
    return _canonical_lpar_score(matches[0].get("curr_lpar_score"))


def _matching_lpar_score_rows(
    value: object, identity: LparIdentity
) -> list[dict[str, Any]]:
    """Return score rows that identify the captured partition."""
    rows = [value] if isinstance(value, dict) else value
    if not isinstance(rows, list):
        return []
    return [row for row in rows if _is_lpar_score_match(row, identity)]


def _is_lpar_score_match(row: object, identity: LparIdentity) -> bool:
    """Return whether a score row names or identifies the captured partition."""
    if not isinstance(row, dict):
        return False
    has_name = "lpar_name" in row
    has_id = "lpar_id" in row
    name_matches = not has_name or row.get("lpar_name") == identity.name
    id_matches = not has_id or str(row.get("lpar_id")) == str(identity.partition_id)
    return name_matches and id_matches and (has_name or has_id)


def _canonical_lpar_score(raw: object) -> int | None:
    """Parse an integer score only from the snapshot's canonical scalar form."""
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return None
    if isinstance(raw, str) and re.fullmatch(r"(?:0|[1-9][0-9]{0,2})", raw) is None:
        return None
    score = int(raw)
    return score if 0 <= score <= 100 else None


async def assess_snapshot_affinity(
    document: str,
    *,
    current_score: int | None,
    predicted_score: int | None,
    policy_state: PolicyState = "absent",
    configured_minimum: int | None = None,
    regression_threshold: int | None = None,
    optimization_threshold: int | None = None,
    stale_after_seconds: int = 86400,
    assessed_at: datetime | None = None,
) -> AffinityAssessmentResult:
    """Assess explicit current evidence without blocking the caller's event loop."""
    return await asyncio.to_thread(
        _assess_snapshot_affinity,
        document,
        current_score=current_score,
        predicted_score=predicted_score,
        policy_state=policy_state,
        configured_minimum=configured_minimum,
        regression_threshold=regression_threshold,
        optimization_threshold=optimization_threshold,
        stale_after_seconds=stale_after_seconds,
        assessed_at=assessed_at,
    )


def _assess_snapshot_affinity(
    document: str,
    *,
    current_score: int | None,
    predicted_score: int | None,
    policy_state: PolicyState,
    configured_minimum: int | None,
    regression_threshold: int | None,
    optimization_threshold: int | None,
    stale_after_seconds: int,
    assessed_at: datetime | None,
) -> AffinityAssessmentResult:
    snapshot = parse_snapshot(document)
    captured_policy = snapshot.observations.minimum_affinity_policy
    policy_capability = next(
        (
            capability
            for capability in snapshot.capabilities
            if capability.name == "minimum-affinity-policy"
        ),
        None,
    )
    if policy_capability is None:
        captured_policy_state = "missing"
    elif not policy_capability.supported:
        captured_policy_state = "unsupported"
    elif captured_policy is None:
        captured_policy_state = "absent"
    else:
        captured_policy_state = "configured"
    captured_minimum = (
        captured_policy.data.get("min_affinity_score")
        if captured_policy is not None
        else None
    )
    return assess_affinity(
        AffinityAssessmentInput(
            captured_score=_captured_lpar_score(snapshot),
            current_score=current_score,
            predicted_score=predicted_score,
            policy_state=policy_state,
            captured_policy_state=captured_policy_state,
            configured_minimum=configured_minimum,
            captured_minimum=captured_minimum,
            captured_at=snapshot.captured_at,
            assessed_at=assessed_at or _utcnow(),
            stale_after_seconds=stale_after_seconds,
            regression_threshold=regression_threshold,
            optimization_threshold=optimization_threshold,
        )
    )


async def validate_lpar_snapshot(document: str) -> dict[str, object]:
    """Validate local snapshot JSON without blocking the caller's event loop.

    Raises:
        SnapshotValidationError: If the document is malformed or violates the snapshot contract.
    """
    snapshot = await asyncio.to_thread(parse_snapshot, document)
    return {"valid": True, "format": snapshot.format, "version": snapshot.version}


async def inspect_lpar_snapshot(document: str) -> SnapshotInspection:
    """Inspect local snapshot identity without blocking the caller's event loop.

    Raises:
        SnapshotValidationError: If the document is malformed or violates the snapshot contract.
    """
    return await asyncio.to_thread(inspect_snapshot, document)


def _resource(entry: dict[str, Any] | None, label: str) -> dict[str, Any]:
    if entry is None or not isinstance(entry.get("Resource"), dict):
        raise ValueError(f"Snapshot capture cannot read {label} resource metadata")
    return entry["Resource"]


@overload
def _nonblank_text(
    value: Any, label: str, *, optional: Literal[False] = False
) -> str: ...


@overload
def _nonblank_text(
    value: Any, label: str, *, optional: Literal[True]
) -> str | None: ...


def _nonblank_text(value: Any, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Snapshot capture requires nonblank {label}")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"Snapshot capture requires integer {label}")  # noqa: TRY004 - ValueError is the ADR 0029 exported contract, asserted in tests/unit/test_snapshot_capture.py
    if isinstance(value, str) and not value.isdecimal():
        raise ValueError(f"Snapshot capture requires integer {label}")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Snapshot capture requires integer {label}") from exc
    if result <= 0:
        raise ValueError(f"Snapshot capture requires positive {label}")
    return result


def _runtime_int(value: Any, label: str) -> int | None:
    if isinstance(value, bool):
        raise ValueError(f"Snapshot capture requires integer {label}")  # noqa: TRY004 - ValueError is the ADR 0029 exported contract, asserted in tests/unit/test_snapshot_capture.py
    if value in (None, 0, "0"):
        return None
    return _positive_int(value, label)


def _runtime_float(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"Snapshot capture requires numeric {label}")  # noqa: TRY004 - ValueError is the ADR 0029 exported contract, asserted in tests/unit/test_snapshot_capture.py
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Snapshot capture requires numeric {label}") from exc
    if result == 0:
        return None
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"Snapshot capture requires positive finite {label} or zero")
    return result


def _container(resource: dict[str, Any], name: str) -> dict[str, Any]:
    value = resource.get(name)
    return value if isinstance(value, dict) else {}


def _placement(resource: dict[str, Any]) -> dict[str, object]:
    """Read the running allocation from the nested V10R3 configuration containers."""
    processors = _container(resource, "PartitionProcessorConfiguration")
    dedicated_value = processors.get("CurrentHasDedicatedProcessors")
    if dedicated_value in (True, "true"):
        mode = "dedicated"
    elif dedicated_value in (False, "false"):
        mode = "shared"
    else:
        raise ValueError(
            "Snapshot capture requires true/false CurrentHasDedicatedProcessors"
        )
    memory = _container(resource, "PartitionMemoryConfiguration").get("CurrentMemory")
    units = (
        _container(processors, "CurrentSharedProcessorConfiguration").get(
            "CurrentProcessingUnits"
        )
        if mode == "shared"
        else None
    )
    dedicated = (
        _container(processors, "CurrentDedicatedProcessorConfiguration").get(
            "CurrentProcessors"
        )
        if mode == "dedicated"
        else None
    )
    return {
        "state": _nonblank_text(resource.get("PartitionState"), "LPAR state"),
        "rmc_state": _nonblank_text(
            resource.get("ResourceMonitoringControlState"), "RMC state", optional=True
        ),
        "processor_mode": mode,
        "current_memory_mib": _runtime_int(memory, "current memory"),
        "current_processor_units": _runtime_float(units, "current processor units"),
        "dedicated_processors": _runtime_int(dedicated, "dedicated processors"),
    }


@dataclass(frozen=True)
class _AffinityObservations:
    current_lpar: Any
    current_system: Any
    predicted_lpars: Any
    predicted_system: Any
    current_groups: Any
    predicted_groups: Any
    minimum_policy: Any


async def _collect_affinity_observations(
    hmc: HMCClient, system_name: str, lpar_name: str
) -> _AffinityObservations:
    """Collect the affinity evidence used by a snapshot."""
    return _AffinityObservations(
        current_lpar=await get_lpar_memopt_score(
            hmc, lpar_name, system_name_or_uuid=system_name
        ),
        current_system=await get_system_memopt_score(hmc, system_name),
        predicted_lpars=await plan_lpar_memopt_scores(hmc, system_name),
        predicted_system=await plan_system_memopt_score(hmc, system_name),
        current_groups=await list_resource_group_memopt_scores(hmc, system_name),
        predicted_groups=await plan_resource_group_memopt_scores(hmc, system_name),
        minimum_policy=await get_minimum_affinity_policy(
            hmc, lpar_name, system_name_or_uuid=system_name
        ),
    )


def _identity_parts(
    console: dict[str, Any] | None,
    console_resource: dict[str, Any],
    system: dict[str, Any] | None,
    system_resource: dict[str, Any],
    lpar: dict[str, Any] | None,
    lpar_resource: dict[str, Any],
) -> tuple[HMCIdentity, SystemIdentity, LparIdentity]:
    """Parse remote identity fields and enforce the portable snapshot contract."""
    mtms = mtms_parts(system_resource)
    if mtms is None:
        raise ValueError("Snapshot capture requires a complete system MTMS")
    machine_type, model, serial = mtms
    machine_type_model = f"{machine_type}-{model}"
    console_name = leaf_text(console_resource.get("ManagementConsoleName"))
    hmc_name = console_name.strip() if isinstance(console_name, str) else ""
    level = console_version(console_resource)
    return (
        HMCIdentity(
            uuid=_nonblank_text(console.get("UUID") if console else None, "HMC UUID"),
            name=hmc_name or None,
            version=None if level is None else "V{}R{}M{}".format(*level),
        ),
        SystemIdentity(
            uuid=_nonblank_text(system.get("UUID") if system else None, "system UUID"),
            name=_nonblank_text(
                system_resource.get("SystemName"), "system name", optional=True
            ),
            machine_type_model=machine_type_model,
            serial=serial,
        ),
        LparIdentity(
            uuid=_nonblank_text(lpar.get("UUID") if lpar else None, "LPAR UUID"),
            name=_nonblank_text(lpar_resource.get("PartitionName"), "LPAR name"),
            partition_id=_positive_int(
                lpar_resource.get("PartitionID"), "partition ID"
            ),
        ),
    )


async def capture_lpar_snapshot(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile_name: str,
) -> LparSnapshot:
    """Capture one validated portable version-1 LPAR snapshot."""
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    system_name = await resolve_system_name(hmc, system_name_or_uuid)
    lpar_uuid = await resolve_lpar_uuid(
        hmc, lpar_name_or_uuid, system_name_or_uuid=system_uuid
    )
    console = await hmc.get_console_info()
    system = await operating_system_entry(hmc, system_uuid, "the snapshot identity")
    lpar = await hmc.get_logical_partition(lpar_uuid)
    console_resource = _resource(console, "HMC")
    system_resource = _resource(system, "managed-system")
    lpar_resource = _resource(lpar, "LPAR")
    lpar_name = _nonblank_text(lpar_resource.get("PartitionName"), "LPAR name")
    native_data = await read_lpar_profile_record(
        hmc.config, system_name, lpar_name, profile_name
    )
    normalized: NormalizedConfiguration = _normalized_from_profile(
        _parse_profile(native_data)
    )
    observed_at = _utcnow()
    affinity = await _collect_affinity_observations(hmc, system_name, lpar_name)
    hmc_identity, system_identity, lpar_identity = _identity_parts(
        console, console_resource, system, system_resource, lpar, lpar_resource
    )
    snapshot = LparSnapshot(
        format="hmcpctl.lpar-snapshot",
        version=1,
        captured_at=_utcnow(),
        source=SnapshotSource(
            hmc=hmc_identity,
            system=system_identity,
            lpar=lpar_identity,
        ),
        capabilities=(
            SnapshotCapability(
                name="affinity-scores", version=1, supported=True, collection="hmc-cli"
            ),
            SnapshotCapability(
                name="lpar-profile-record",
                version=1,
                supported=True,
                collection="hmc-cli",
            ),
            SnapshotCapability(
                name="minimum-affinity-policy",
                version=1,
                supported=affinity.minimum_policy.capability == "available",
                collection="hmc-cli",
                unavailable_reason=affinity.minimum_policy.unavailable_reason,
            ),
            SnapshotCapability(
                name="runtime-placement",
                version=1,
                supported=True,
                collection="hmc-rest",
            ),
        ),
        configuration=SnapshotConfiguration(
            profile_name=profile_name,
            native=NativeProfile(media_type=PROFILE_MEDIA_TYPE, data=native_data),
            normalized=normalized,
        ),
        observations=SnapshotObservations(
            observed_at=observed_at,
            runtime_placement=ObservationEnvelope(
                media_type=PLACEMENT_MEDIA_TYPE, data=_placement(lpar_resource)
            ),
            scores=ObservationEnvelope(
                media_type=SCORES_MEDIA_TYPE,
                data={
                    "current": {
                        "lpar": affinity.current_lpar,
                        "system": affinity.current_system,
                    },
                    "predicted": {
                        "lpars": affinity.predicted_lpars,
                        "system": affinity.predicted_system,
                    },
                    "resource_groups": {
                        "current": asdict(affinity.current_groups),
                        "predicted": asdict(affinity.predicted_groups),
                    },
                },
            ),
            minimum_affinity_policy=(
                ObservationEnvelope(
                    media_type=MINIMUM_AFFINITY_POLICY_MEDIA_TYPE,
                    data={
                        "min_affinity_score": affinity.minimum_policy.min_affinity_score,
                        "min_affinity_score_action": (
                            affinity.minimum_policy.min_affinity_score_action
                        ),
                    },
                )
                if affinity.minimum_policy.capability == "available"
                else None
            ),
        ),
    )
    serialize_snapshot(snapshot)
    return snapshot
