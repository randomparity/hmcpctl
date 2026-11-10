"""Freshness, release snapshots and runtime capability projections."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from capability_evidence import closure as capability_closure
from capability_evidence import schema as capability_schema
from capability_evidence.registry import RegistryTool
from capability_evidence.schema import InventoryError

STALE_AFTER_DAYS = 90


def gap_is_current(
    confirmation: Mapping[str, Any],
    environment: tuple[str, str],
    fingerprint: str,
    now: datetime | None = None,
) -> bool:
    """Reuse a validated confirmation only in its environment, closure and 90-day window."""
    age = (now or datetime.now(UTC)) - datetime.fromisoformat(
        confirmation["observed_at"]
    )
    return (
        (confirmation["hmc_release"], confirmation["hardware_family"]) == environment
        and confirmation["closure_fingerprint"] == fingerprint
        and timedelta(0) <= age <= timedelta(days=STALE_AFTER_DAYS)
    )


@dataclass(frozen=True)
class OperationState:
    """One operation's derived verification state, and why it is stale."""

    state: str
    reason: str | None = None
    implementation: str | None = None
    observed_at: str | None = None


def _handler_module(handler: str) -> str:
    """The module holding a registry handler, dropping the function component.

    The composed `hmc_effective_permissions` record ends in `<composed>` rather
    than a function name, and the same rule resolves it.
    """
    return handler.rsplit(".", 1)[0]


def _stale_reason(
    observation: Mapping[str, object],
    fingerprint: str,
    now: datetime,
    *,
    age_staleness: bool,
) -> str | None:
    """Which staleness trigger fired, most specific first, or None."""
    if observation.get("closure_fingerprint") != fingerprint:
        return "closure-changed"
    try:
        observed = datetime.fromisoformat(str(observation.get("observed_at")))
    except ValueError:
        return "age-exceeded"
    if age_staleness and now - observed > timedelta(days=STALE_AFTER_DAYS):
        return "age-exceeded"
    return None


def derive_states(
    records: Sequence[Mapping[str, object]],
    registry: Collection[RegistryTool],
    repo_root: Path,
    now: datetime,
    *,
    age_staleness: bool = True,
    _fingerprints: dict[tuple[Path, str], str] | None = None,
) -> dict[str, OperationState]:
    """Derive every operation's verification state; never stored, never an error."""
    by_operation = {
        record["operation"]: record
        for record in records
        if isinstance(record.get("operation"), str)
    }
    # Standalone calls are fresh; main shares this only across its two derivations.
    fingerprints = {} if _fingerprints is None else _fingerprints
    states: dict[str, OperationState] = {}
    for tool in registry:
        record = by_operation.get(tool.operation)
        if record is None:
            states[tool.operation] = OperationState("unrecorded")
            continue
        implementation = record.get("implementation")
        state = (
            implementation.get("state") if isinstance(implementation, dict) else None
        )
        implementation_state = state if isinstance(state, str) else None
        evidence = record.get("evidence")
        observations = [
            observation
            for observation in (evidence if isinstance(evidence, list) else [])
            if isinstance(observation, dict) and observation.get("channel") == "live"
        ]
        if not observations:
            states[tool.operation] = OperationState(
                "unevidenced", implementation=implementation_state
            )
            continue
        # An operation keeps one live row per producing observation (ADR 0212).
        # The newest row decides its state, ordered by the key promotion selects
        # with, so a timestamp tie between producers resolves the same way.
        latest = max(
            observations,
            key=lambda item: (str(item.get("observed_at")), str(item.get("id"))),
        )
        key = (repo_root, _handler_module(tool.handler))
        if key not in fingerprints:
            fingerprints[key] = capability_closure.closure_fingerprint(*key)
        reason = _stale_reason(
            latest,
            fingerprints[key],
            now,
            age_staleness=age_staleness,
        )
        states[tool.operation] = OperationState(
            "stale"
            if reason
            else ("current" if latest.get("result") == "passed" else "failed"),
            reason,
            implementation_state,
            str(latest.get("observed_at")),
        )
    return states


def load_release_verification(path: Path) -> dict[str, dict[str, object]]:
    """Validate the historical snapshot before using any frozen evidence."""
    try:
        with path.open("rb") as source:
            content = source.read(1_048_577)
        if len(content) > 1_048_576:
            raise InventoryError("release verification snapshot exceeds 1 MiB")
        document = json.loads(
            content.decode("utf-8"), object_pairs_hook=capability_schema.unique_pairs
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InventoryError(
            f"cannot read release verification snapshot {path}: {error}"
        ) from error
    if not isinstance(document, dict) or set(document) != {
        "format_version",
        "operations",
    }:
        raise InventoryError(
            "release verification snapshot must contain format_version and operations"
        )
    if type(document["format_version"]) is not int or document["format_version"] != 1:
        raise InventoryError(
            "release verification snapshot format_version must be integer 1"
        )
    if not isinstance(document["operations"], list):
        raise InventoryError(
            "release verification snapshot operations must be an array"
        )
    result: dict[str, dict[str, object]] = {}
    for row in document["operations"]:
        _validate_release_row(row)
        operation = row["operation"]
        if operation in result:
            raise InventoryError(
                f"duplicate release verification operation {operation!r}"
            )
        result[operation] = row
    if list(result) != sorted(result):
        raise InventoryError("release verification operations must be sorted")
    return result


def _validate_release_row(row: Any) -> None:
    keys = {"operation", "verification", "reason", "observed_at"}
    if not isinstance(row, dict) or set(row) != keys:
        raise InventoryError(
            f"release verification row must contain exactly {sorted(keys)}"
        )
    operation, state, reason, timestamp = (
        row[key] for key in ("operation", "verification", "reason", "observed_at")
    )
    if (
        not isinstance(operation, str)
        or re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", operation) is None
    ):
        raise InventoryError("release verification row has invalid operation ID")
    if not isinstance(state, str) or state not in {
        "unevidenced",
        "current",
        "stale",
        "failed",
    }:
        raise InventoryError(f"{operation}: invalid release verification state")
    if reason is not None and (
        not isinstance(reason, str) or reason not in {"closure-changed", "age-exceeded"}
    ):
        raise InventoryError(f"{operation}: invalid release verification reason")
    if (state == "stale") != (reason is not None):
        raise InventoryError(f"{operation}: inconsistent release verification reason")
    if timestamp is not None:
        if (
            not isinstance(timestamp, str)
            or capability_schema.TIMESTAMP.fullmatch(timestamp) is None
        ):
            raise InventoryError(f"{operation}: invalid release verification timestamp")
        try:
            datetime.fromisoformat(timestamp)
        except ValueError as error:
            raise InventoryError(
                f"{operation}: invalid release verification timestamp"
            ) from error
    if (state == "unevidenced") != (timestamp is None):
        raise InventoryError(
            f"{operation}: inconsistent release verification timestamp"
        )


def render_release_verification(
    records: Sequence[Mapping[str, object]],
    registry: Collection[RegistryTool],
    repo_root: Path,
    now: datetime,
    *,
    _fingerprints: dict[tuple[Path, str], str] | None = None,
) -> str:
    """Derive a release snapshot from candidate closures, without age decay."""
    recorded = {
        str(record["operation"])
        for record in records
        if isinstance(record.get("operation"), str)
    }
    states = derive_states(
        records,
        registry,
        repo_root,
        now,
        age_staleness=False,
        _fingerprints=_fingerprints,
    )
    operations = [
        {
            "operation": operation,
            "verification": states[operation].state,
            "reason": states[operation].reason,
            "observed_at": states[operation].observed_at,
        }
        for operation in sorted(recorded & states.keys())
    ]
    return (
        json.dumps(
            {
                "format_version": 1,
                "operations": operations,
            },
            indent=2,
        )
        + "\n"
    )


def render_runtime_projection(
    records: Sequence[Mapping[str, object]],
    registry: Collection[RegistryTool],
    snapshot: Mapping[str, Mapping[str, object]],
) -> str:
    """Combine current catalog structure with the release's frozen evidence."""
    registered = {tool.operation for tool in registry}
    operations = []
    for record in sorted(records, key=lambda row: str(row["operation"])):
        operation = str(record["operation"])
        if operation not in registered:
            continue
        implementation = record["implementation"]
        assert isinstance(implementation, dict)
        frozen = snapshot.get(operation, {})
        operations.append(
            {
                "operation": operation,
                "implementation": implementation["state"],
                "verification": frozen.get("verification", "unevidenced"),
                "reason": frozen.get("reason"),
                "observed_at": frozen.get("observed_at"),
            }
        )
    return (
        json.dumps(
            {
                "format_version": 1,
                "runtime_eligibility": "existing-runtime-guards",
                "operations": operations,
            },
            indent=2,
        )
        + "\n"
    )


def write_runtime_projection(path: Path, document: str) -> None:
    """Atomically replace the generated runtime projection."""
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(document)
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
