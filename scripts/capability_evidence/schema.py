"""Closed schemas and privacy grammars for capability evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeGuard

MATURITY_STATES = {"absent", "partial", "implemented"}
EVIDENCE_CHANNELS = {"contract-review", "automated", "live"}
EVIDENCE_RESULTS = {"failed", "passed"}
SHA_1 = re.compile(r"[0-9a-f]{40}")
SHA_256 = re.compile(r"[0-9a-f]{64}")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
#: `maturity.json` alone moves to format 4 (ADR 0186, after ADR 0132's format 3);
#: the other three catalogs are unchanged and stay at 1.
MATURITY_FORMAT_VERSION = 4
#: The exact key set of a format 4 observation. There is one observation shape:
#: ADR 0126's `not-run` placeholder is dropped rather than carried forward.
ATTEMPTED_KEYS = {
    "id",
    "channel",
    "result",
    "scenario",
    "tested_commit",
    "observed_at",
    "hmc_release",
    "hardware_family",
    "schema_version",
    "cleanup",
    "closure_fingerprint",
    "assertions",
}
CLEANUP = {"not-run", "not-required", "failed", "passed"}
OBSERVATION_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
SCENARIO_ID = re.compile(r"st\d+-[a-z0-9-]+")
ASSERTION_ID = re.compile(r"[a-z][a-z0-9-]{1,62}[a-z0-9]")
GAP_VARIANT = re.compile(r"[a-z][a-z0-9-]{0,63}")
CONFIRMATION_KEYS = {
    "tested_commit",
    "observed_at",
    "hmc_release",
    "hardware_family",
    "closure_fingerprint",
}
#: Narrow grammars, not a permissive character class with an address rejection:
#: `[A-Za-z0-9 ._-]{0,39}` admits a hostname, a serial and a location code
#: verbatim while rejecting only a dotted quad.
HMC_RELEASE = re.compile(r"V\d+R\d+(?:M\d+)?")
HARDWARE_FAMILY = re.compile(r"POWER\d+")
#: What a run may record for `HMC_SCHEMA_VERSION` (ADR 0186): the header token, or
#: the run header's own rendering of an unset or empty variable.
SCHEMA_VERSION = re.compile(r"V\d+(?:_\d+)+|\(not set\)")
#: Carried by observations stored before format 4; the runner never writes it.
UNRECORDED_SCHEMA_VERSION = "unrecorded"


class InventoryError(ValueError):
    """An inventory artifact cannot be parsed safely."""


def unique_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InventoryError(f"duplicate key {key!r}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs
        )
    except (OSError, UnicodeError, json.JSONDecodeError, InventoryError) as error:
        raise InventoryError(f"{path}: {error}") from error
    if not isinstance(value, dict):
        raise InventoryError(f"{path}: root must be an object")
    return value


def array(document: Mapping[str, object], key: str, errors: list[str]) -> list[object]:
    value = document.get(key)
    if not isinstance(value, list):
        errors.append(f"{key}: expected array")
        return []
    return value


def objects(
    values: Sequence[object], key: str, errors: list[str]
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            errors.append(f"{key}[{index}]: expected object")
        else:
            result.append(value)
    return result


def exact_keys(
    value: object, keys: set[str], label: str, errors: list[str]
) -> TypeGuard[dict[str, object]]:
    if not isinstance(value, dict) or set(value) != keys:
        errors.append(f"{label}: expected exactly {sorted(keys)}")
        return False
    return True


def nonempty(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip())


def one_of(value: object, choices: set[str]) -> bool:
    return isinstance(value, str) and value in choices


def matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def scope_identity(
    value: object, label: str, errors: list[str], *, missing: bool = False
) -> tuple[object, ...] | None:
    keys = {"variant", "parameters"}
    if missing and isinstance(value, dict) and "confirmation" in value:
        keys.add("confirmation")
        validate_confirmation(value["confirmation"], label, errors)
        if value.get("parameters") != [] or not matches(
            GAP_VARIANT, value.get("variant")
        ):
            errors.append(
                f"{label}: confirmed gaps require a closed variant and empty parameters"
            )
    if not exact_keys(value, keys, label, errors):
        return None
    variant = value["variant"]
    parameters = value["parameters"]
    if not nonempty(variant) or not isinstance(parameters, list):
        errors.append(f"{label}: invalid variant or parameters")
        return None
    pairs: list[tuple[str, str]] = []
    for index, parameter in enumerate(parameters):
        parameter_label = f"{label} parameter {index}"
        if not exact_keys(parameter, {"name", "constraint"}, parameter_label, errors):
            continue
        name, constraint = parameter["name"], parameter["constraint"]
        if not nonempty(name) or not nonempty(constraint):
            errors.append(f"{parameter_label}: name and constraint are required")
        else:
            pairs.append((name, constraint))
    if len({name for name, _ in pairs}) != len(pairs) or pairs != sorted(pairs):
        errors.append(f"{label}: parameters must be unique and sorted")
    return (variant, tuple(pairs))


def _validate_implementation(
    record: dict[str, object], label: str, errors: list[str]
) -> set[tuple[object, ...]]:
    implementation = record.get("implementation")
    if not exact_keys(
        implementation, {"state", "implemented_scope", "missing_scope"}, label, errors
    ):
        return set()
    state = implementation["state"]
    implemented = implementation["implemented_scope"]
    missing = implementation["missing_scope"]
    if (
        not one_of(state, MATURITY_STATES)
        or not isinstance(implemented, list)
        or not isinstance(missing, list)
    ):
        errors.append(f"{label}: invalid implementation state or scope lists")
        return set()
    implemented_ids = {scope_identity(scope, label, errors) for scope in implemented}
    missing_ids = {
        scope_identity(scope, label, errors, missing=True) for scope in missing
    }
    implemented_ids = {identity for identity in implemented_ids if identity is not None}
    missing_ids = {identity for identity in missing_ids if identity is not None}
    if len(implemented_ids) != len(implemented) or len(missing_ids) != len(missing):
        errors.append(f"{label}: scope entries must be unique and valid")
    valid_lists = {
        "absent": (not implemented and bool(missing)),
        "partial": (bool(implemented) and bool(missing)),
        "implemented": (bool(implemented) and not missing),
    }
    if not valid_lists.get(state, False) or implemented_ids & missing_ids:
        errors.append(f"{label}: contradictory implementation scope")
    return implemented_ids


def _validate_timestamp(
    value: object, label: str, errors: list[str]
) -> datetime | None:
    if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
        errors.append(f"{label}: observed_at must be canonical UTC")
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        errors.append(f"{label}: observed_at is not a calendar timestamp")
        return None


def validate_confirmation(value: object, label: str, errors: list[str]) -> None:
    if not exact_keys(value, CONFIRMATION_KEYS, label, errors):
        return
    for field, pattern in (
        ("tested_commit", SHA_1),
        ("closure_fingerprint", SHA_256),
        ("hmc_release", HMC_RELEASE),
        ("hardware_family", HARDWARE_FAMILY),
    ):
        if not matches(pattern, value[field]):
            errors.append(f"{label}: invalid confirmation {field}")
    observed_at = _validate_timestamp(value["observed_at"], label, errors)
    if observed_at is not None and observed_at > datetime.now(UTC):
        errors.append(f"{label}: confirmation observed_at must not be in the future")


def validate_observation(
    observation: object,
    operation: str,
    evidence_ids: set[str],
    errors: list[str],
) -> None:
    """Validate one format 4 observation against its single closed shape.

    `maturity.json` can be promoted from runner output or hand-edited, so
    every bound the runner applies on the way out is applied again here: the
    validator is the only check a hand-authored record ever meets.
    """
    if not exact_keys(
        observation, ATTEMPTED_KEYS, f"maturity operation {operation} evidence", errors
    ):
        return
    identity = observation["id"]
    label = (
        f"maturity evidence {identity}"
        if isinstance(identity, str)
        else f"maturity operation {operation} evidence"
    )
    if not matches(OBSERVATION_ID, identity):
        errors.append(f"{label}: id must match {OBSERVATION_ID.pattern}")
        return
    if identity in evidence_ids:
        errors.append(f"{label}: id must be catalog-wide unique")
        return
    assert isinstance(identity, str)
    evidence_ids.add(identity)
    if not one_of(observation["channel"], EVIDENCE_CHANNELS) or not one_of(
        observation["result"], EVIDENCE_RESULTS
    ):
        errors.append(f"{label}: invalid channel or result")
        return
    if not matches(SCENARIO_ID, observation["scenario"]):
        errors.append(f"{label}: scenario must match st<n>-<slug>")
    if not matches(SHA_1, observation["tested_commit"]):
        errors.append(f"{label}: tested_commit must be a full SHA")
    if not matches(SHA_256, observation["closure_fingerprint"]):
        errors.append(f"{label}: closure_fingerprint must be a full SHA-256")
    _validate_timestamp(observation["observed_at"], label, errors)
    if not matches(HMC_RELEASE, observation["hmc_release"]) or not matches(
        HARDWARE_FAMILY, observation["hardware_family"]
    ):
        errors.append(f"{label}: environment values do not match their grammar")
    schema_version = observation["schema_version"]
    if schema_version != UNRECORDED_SCHEMA_VERSION and not matches(
        SCHEMA_VERSION, schema_version
    ):
        errors.append(
            f"{label}: schema_version must be V<n>_<n>[_<n>...], (not set) or unrecorded"
        )
    if not one_of(observation["cleanup"], CLEANUP):
        errors.append(f"{label}: invalid cleanup")
    _validate_assertions(observation, label, errors)


def _validate_assertions(
    observation: dict[str, object], label: str, errors: list[str]
) -> None:
    """Validate the held-assertion list and what a `passed` observation requires."""
    assertions = observation["assertions"]
    if not isinstance(assertions, list) or not all(
        matches(ASSERTION_ID, item) for item in assertions
    ):
        errors.append(f"{label}: assertions must be closed-shape tokens")
        return
    if len(set(assertions)) != len(assertions):
        errors.append(f"{label}: assertions must be unique")
    if observation["result"] != "passed":
        return
    if not assertions:
        errors.append(f"{label}: passed evidence requires assertions")
    if not one_of(observation["cleanup"], {"passed", "not-required"}):
        errors.append(f"{label}: a passed observation requires successful cleanup")


def validate_maturity(
    records: Sequence[dict[str, object]],
    operation_ids: Collection[str],
    errors: list[str],
) -> None:
    seen: set[str] = set()
    evidence_ids: set[str] = set()
    for record in records:
        operation = record.get("operation")
        label = f"maturity operation {operation}"
        if not exact_keys(
            record, {"operation", "implementation", "evidence"}, label, errors
        ):
            continue
        if not nonempty(operation):
            errors.append(f"{label}: operation must be a non-empty string")
            continue
        if operation in seen:
            errors.append(f"{label}: duplicate operation")
        elif operation not in operation_ids:
            errors.append(f"{label}: unknown operation")
        seen.add(operation)
        _validate_implementation(record, label, errors)
        evidence = record["evidence"]
        if not isinstance(evidence, list):
            errors.append(f"{label}: evidence must be a list")
            continue
        for observation in evidence:
            validate_observation(observation, str(operation), evidence_ids, errors)
