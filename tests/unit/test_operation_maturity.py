"""Contract tests for the packaged operation-maturity projection."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import hmcpctl.operation_maturity as maturity


def _projection(
    *,
    operation: str = "system.list",
    implementation: str = "implemented",
    verification: str = "current",
    reason: str | None = None,
    observed_at: str | None = "2026-09-01T12:00:00Z",
) -> dict[str, object]:
    return {
        "format_version": 1,
        "runtime_eligibility": "existing-runtime-guards",
        "operations": [
            {
                "operation": operation,
                "implementation": implementation,
                "verification": verification,
                "reason": reason,
                "observed_at": observed_at,
            }
        ],
    }


def _install_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: object
) -> None:
    (tmp_path / "_operation_maturity.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    monkeypatch.setattr(maturity.resources, "files", lambda _package: tmp_path)


def test_catalog_is_sparse_and_values_are_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_projection(tmp_path, monkeypatch, _projection())

    catalog = maturity.operation_maturity_catalog()

    assert set(catalog) == {"system.list"}
    assert catalog["system.list"] == maturity.OperationMaturity(
        implementation="implemented",
        verification="current",
        runtime_eligibility="existing-runtime-guards",
    )
    with pytest.raises(TypeError):
        catalog["other.read"] = catalog["system.list"]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        catalog["system.list"].verification = "failed"  # type: ignore[misc]


def test_absent_valid_operation_resolves_to_explicit_unrecorded_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_projection(tmp_path, monkeypatch, _projection())

    result = maturity.operation_maturity("vnic.add")

    assert result == maturity.OperationMaturity(
        implementation="unrecorded",
        verification="unrecorded",
        runtime_eligibility="existing-runtime-guards",
    )
    assert maturity.operation_maturity_meta("vnic.add") == {
        "implementation": "unrecorded",
        "verification": "unrecorded",
        "runtime_eligibility": "existing-runtime-guards",
    }


@pytest.mark.parametrize(
    "operation", ["", "system", "System.list", "system/list", "system.list.extra"]
)
def test_invalid_operation_id_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    _install_projection(tmp_path, monkeypatch, _projection())

    with pytest.raises(maturity.OperationMaturityError, match="operation ID"):
        maturity.operation_maturity(operation)


@pytest.mark.parametrize("verification", ["current", "failed"])
def test_verification_remains_frozen_after_ninety_days(
    tmp_path, monkeypatch, verification
):
    _install_projection(
        tmp_path,
        monkeypatch,
        _projection(verification=verification, observed_at="2000-01-01T00:00:00Z"),
    )
    assert maturity.operation_maturity("system.list").verification == verification


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"unexpected": True}),
        lambda value: value["operations"][0].update({"unexpected": True}),
        lambda value: value["operations"][0].pop("reason"),
        lambda value: value.update({"format_version": True}),
        lambda value: value.update({"runtime_eligibility": "promoted"}),
        lambda value: value["operations"][0].update({"operation": "System.list"}),
        lambda value: value["operations"][0].update({"implementation": []}),
        lambda value: value["operations"][0].update({"verification": {}}),
        lambda value: value["operations"][0].update({"reason": []}),
        lambda value: value["operations"][0].update({"observed_at": "not-a-time"}),
        lambda value: value["operations"][0].update({"verification": "unevidenced"}),
        lambda value: value["operations"][0].update({"reason": "age-exceeded"}),
        lambda value: value["operations"].append(value["operations"][0].copy()),
    ],
)
def test_projection_schema_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate
) -> None:
    document = _projection()
    mutate(document)
    _install_projection(tmp_path, monkeypatch, document)

    with pytest.raises(maturity.OperationMaturityError):
        maturity.operation_maturity_catalog()


def test_projection_rejects_duplicate_json_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "_operation_maturity.json").write_text(
        '{"format_version":1,"format_version":1,'
        '"runtime_eligibility":"existing-runtime-guards","operations":[]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(maturity.resources, "files", lambda _package: tmp_path)

    with pytest.raises(maturity.OperationMaturityError, match="duplicate key"):
        maturity.operation_maturity_catalog()


@pytest.mark.parametrize("kind", ["lpar", "vios"])
@pytest.mark.parametrize(
    "operation", ["get_security_policy", "configure_security_policy"]
)
def test_security_policy_implementation_does_not_claim_hardware_evidence(
    kind, operation
):
    result = maturity.operation_maturity(f"{kind}.{operation}")
    assert result.implementation == "implemented"
    assert result.verification == "unevidenced"
