"""Contract tests for the capability-inventory validator."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).parents[2]
MODULE_PATH = ROOT / "scripts" / "check_capability_inventory.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("check_capability_inventory", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
inventory = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = inventory
SPEC.loader.exec_module(inventory)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _digest(character: str) -> str:
    return "-".join([character * 8] * 8)


def _minimal_inventory(root: Path) -> None:
    root.mkdir(exist_ok=True)
    (root / "releases").mkdir(exist_ok=True)
    _write_json(root / "releases/v0.0.0.json", {"format_version": 1, "operations": []})
    _write_json(
        root / "release-verification.json", {"format_version": 1, "operations": []}
    )
    _write_json(
        root / "corpora.json",
        {
            "format_version": 1,
            "corpora": [
                {
                    "id": "commands-p10",
                    "source_url": "https://example.test/commands",
                    "archive_sha256": _digest("a"),
                    "captured_pages": 1,
                    "navigation_pages": 0,
                }
            ],
            "topics": [
                {
                    "id": "commands-p10:alpha",
                    "corpus": "commands-p10",
                    "path": "alpha.md",
                    "source_url": "https://example.test/alpha",
                    "captured": "2026-08-23T00:00:00Z",
                    "sha256": _digest("b"),
                    "classification": "operation",
                    "reason": "command reference",
                }
            ],
            "source_units": [
                {
                    "id": "commands-p10:alpha:L10",
                    "topic": "commands-p10:alpha",
                    "kind": "command-option",
                    "line": 10,
                    "sha256": _digest("c"),
                    "text": "--name | resource name",
                    "accounting": {"kind": "row", "id": "cli:alpha"},
                }
            ],
        },
    )
    _write_json(
        root / "rows.json",
        {
            "format_version": 1,
            "rows": [
                {
                    "id": "cli:alpha",
                    "kind": "cli-command",
                    "name": "alpha",
                    "modes": [],
                    "parameters": ["commands-p10:alpha:L10"],
                    "source_units": ["commands-p10:alpha:L10"],
                    "releases": ["power10"],
                    "disposition": {"kind": "coverage-child", "issue": 637},
                }
            ],
        },
    )
    _write_json(
        root / "operations.json",
        {"format_version": 1, "operations": []},
    )
    _write_json(
        root / "maturity.json",
        {
            "format_version": inventory.MATURITY_FORMAT_VERSION,
            "admission_policy": "existing-runtime-guards",
            "operations": [],
        },
    )


def _scope(
    variant: str = "default", parameters: list[dict[str, str]] | None = None
) -> dict[str, object]:
    return {"variant": variant, "parameters": parameters or []}


def _operation(
    operation: str = "system.list", state: str = "implemented"
) -> dict[str, object]:
    scope = _scope()
    return {
        "operation": operation,
        "implementation": {
            "state": state,
            "implemented_scope": [] if state == "absent" else [scope],
            "missing_scope": [scope] if state == "absent" else [],
        },
        "evidence": [],
    }


def _confirmed_gap():
    return {
        "variant": "managed-system-pcm",
        "parameters": [],
        "confirmation": {
            "tested_commit": "a" * 40,
            "observed_at": "2026-09-06T12:00:00Z",
            "hmc_release": "V10R3",
            "hardware_family": "POWER10",
            "closure_fingerprint": "b" * 64,
        },
    }


def test_confirmed_gap_is_valid_missing_scope_without_evidence():
    record = _operation("system.list", "absent")
    record["implementation"]["missing_scope"] = [_confirmed_gap()]
    errors = []
    inventory._validate_maturity([record], {"system.list"}, errors)
    assert errors == []
    assert record["evidence"] == []


def test_confirmed_gap_never_promotes_verification(tmp_path):
    registry = _closure_registry(tmp_path)
    record = _operation("system.list", "absent")
    record["implementation"]["missing_scope"] = [_confirmed_gap()]
    assert (
        inventory.derive_states([record], registry, tmp_path, _NOW)["system.list"].state
        == "unevidenced"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("tested_commit", "x"),
        ("closure_fingerprint", "x"),
        ("observed_at", "invalid"),
        ("observed_at", "9999-01-01T00:00:00Z"),
        ("hmc_release", "host.example.test"),
        ("hardware_family", "machine"),
        ("result", "passed"),
    ],
)
def test_gap_confirmation_rejects_invalid_or_promoting_fields(field, value):
    record = _operation("system.list", "absent")
    gap = _confirmed_gap()
    gap["confirmation"][field] = value
    record["implementation"]["missing_scope"] = [gap]
    errors = []
    inventory._validate_maturity([record], {"system.list"}, errors)
    assert errors


@pytest.mark.parametrize(
    "placement,parameters",
    [
        ("implemented_scope", []),
        ("missing_scope", [{"name": "category", "constraint": "ManagedSystem"}]),
    ],
)
def test_confirmation_rejects_unsupported_scope_identity(placement, parameters):
    record = _operation(
        "system.list", "absent" if placement == "missing_scope" else "implemented"
    )
    gap = _confirmed_gap()
    gap["parameters"] = parameters
    record["implementation"][placement] = [gap]
    errors = []
    inventory._validate_maturity([record], {"system.list"}, errors)
    assert errors


@pytest.mark.parametrize(
    "days,current", [(0, True), (90, True), (91, False), (-1, False)]
)
def test_gap_currency_has_bounded_revalidation_age(days, current):
    from datetime import timedelta

    confirmation = _confirmed_gap()["confirmation"]
    now = datetime.fromisoformat(confirmation["observed_at"]) + timedelta(days=days)
    assert (
        inventory.gap_is_current(confirmation, ("V10R3", "POWER10"), "b" * 64, now)
        is current
    )


@pytest.mark.parametrize(
    "environment,fingerprint",
    [
        (("V10R4", "POWER10"), "b" * 64),
        (("V10R3", "POWER11"), "b" * 64),
        (("V10R3", "POWER10"), "c" * 64),
    ],
)
def test_gap_currency_requires_same_environment_and_closure(environment, fingerprint):
    confirmation = _confirmed_gap()["confirmation"]
    assert not inventory.gap_is_current(
        confirmation, environment, fingerprint, datetime(2026, 9, 7, tzinfo=UTC)
    )


def _observation(
    *,
    identity: str = "st1-hmc-get-console-info",
    result: str = "passed",
    fingerprint: str | None = None,
    observed_at: str = "2026-09-06T12:00:00Z",
) -> dict[str, object]:
    """One format 4 observation: the single closed shape the validator admits."""
    return {
        "id": identity,
        "channel": "live",
        "result": result,
        "scenario": "st1-console-identity",
        "tested_commit": "a" * 40,
        "observed_at": observed_at,
        "hmc_release": "V10R3",
        "hardware_family": "POWER10",
        "schema_version": "(not set)",
        "cleanup": "not-required",
        "closure_fingerprint": fingerprint or "b" * 64,
        "assertions": ["console-uuid-present"],
    }


def _write_maturity(root: Path, records: list[dict[str, object]]) -> None:
    _write_json(
        root / "maturity.json",
        {
            "format_version": inventory.MATURITY_FORMAT_VERSION,
            "admission_policy": "existing-runtime-guards",
            "operations": records,
        },
    )


@pytest.fixture
def registered_inventory(tmp_path: Path) -> tuple[inventory.RegistryTool, ...]:
    _minimal_inventory(tmp_path)
    tool = next(
        tool
        for tool in inventory.discover_registry()
        if tool.operation == "system.list"
    )
    _write_json(
        tmp_path / "operations.json",
        {
            "format_version": 1,
            "operations": [
                {
                    "tool": tool.tool,
                    "operation": tool.operation,
                    "handler": tool.handler,
                    "signature": tool.signature,
                    "surfaces": list(tool.surfaces),
                    "row_ids": [],
                    "composite_reason": "anonymous fixture operation",
                    "tests": [],
                }
            ],
        },
    )
    return (tool,)


def _maturity_report(
    root: Path,
    registry: tuple[inventory.RegistryTool, ...],
    records: list[dict[str, object]],
) -> inventory.Report:
    _write_maturity(root, records)
    return inventory.validate_inventory(root, registry, repo_root=root)


def test_load_json_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"format_version":1,"format_version":1}', encoding="utf-8")
    with pytest.raises(inventory.InventoryError, match="duplicate key.*format_version"):
        inventory.load_json(path)


def test_validation_detects_dangling_and_double_accounted_units(tmp_path: Path) -> None:
    _minimal_inventory(tmp_path)
    rows = json.loads((tmp_path / "rows.json").read_text())
    rows["rows"][0]["source_units"].append("missing-unit")
    rows["rows"].append({**rows["rows"][0], "id": "cli:beta"})
    _write_json(tmp_path / "rows.json", rows)

    report = inventory.validate_inventory(tmp_path, (), repo_root=tmp_path)

    assert any("missing-unit" in error for error in report.errors)
    assert any("accounted by more than one row" in error for error in report.errors)


def test_unknowns_and_proposed_exclusions_prevent_completeness(tmp_path: Path) -> None:
    _minimal_inventory(tmp_path)
    rows = json.loads((tmp_path / "rows.json").read_text())
    rows["rows"][0]["disposition"] = {
        "kind": "unknown",
        "question": "Source does not state whether this is supported.",
    }
    _write_json(tmp_path / "rows.json", rows)

    report = inventory.validate_inventory(tmp_path, (), repo_root=tmp_path)

    assert not report.complete
    assert report.unknown_ids == ("cli:alpha",)
    assert not report.errors


def test_registry_evidence_checks_signature_and_paths(tmp_path: Path) -> None:
    _minimal_inventory(tmp_path)
    operations = {
        "format_version": 1,
        "operations": [
            {
                "tool": "hmc_alpha",
                "operation": "alpha.read",
                "handler": "example.handler",
                "signature": "(name: str)",
                "surfaces": ["mcp"],
                "row_ids": ["cli:alpha"],
                "composite_reason": None,
                "tests": ["tests/missing.py"],
            }
        ],
    }
    _write_json(tmp_path / "operations.json", operations)
    discovered = (
        inventory.RegistryTool(
            tool="hmc_alpha",
            operation="alpha.read",
            handler="example.handler",
            signature="(other: str)",
            surfaces=("mcp",),
        ),
    )

    report = inventory.validate_inventory(tmp_path, discovered, repo_root=tmp_path)

    assert any("signature" in error for error in report.errors)
    assert any("tests/missing.py" in error for error in report.errors)


def test_verify_corpora_detects_changed_and_extra_files(tmp_path: Path) -> None:
    data = tmp_path / "data"
    source = tmp_path / "source"
    data.mkdir()
    source.mkdir()
    page = source / "alpha.md"
    page.write_text(
        "---\nsource: https://example.test/alpha\ncaptured: now\n---\n",
        encoding="utf-8",
    )
    digest = inventory.format_sha256(page.read_bytes())
    _write_json(
        data / "corpora.json",
        {
            "format_version": 1,
            "corpora": [{"id": "commands-p10"}],
            "topics": [
                {
                    "id": "commands-p10:alpha",
                    "corpus": "commands-p10",
                    "path": "alpha.md",
                    "sha256": digest,
                }
            ],
            "source_units": inventory.extract_source_units(
                "commands-p10:alpha", page.read_text()
            ),
        },
    )

    assert inventory.verify_corpora(data, {"commands-p10": source}) == []
    page.write_text("changed\n", encoding="utf-8")
    (source / "extra.md").write_text("extra\n", encoding="utf-8")
    errors = inventory.verify_corpora(data, {"commands-p10": source})
    assert any("hash mismatch" in error for error in errors)
    assert any("unexpected source file" in error for error in errors)


def test_extract_source_units_preserves_both_capture_forms() -> None:
    lower = "---\ncaptured: 2026-08-23T00:00:00Z\n---\n## SYNOPSIS\nalpha --name X\n"
    capital = "# Index\n\nCaptured: 2026-08-23T00:00:00Z\n"

    assert inventory.extract_capture_time(lower) == "2026-08-23T00:00:00Z"
    assert inventory.extract_capture_time(capital) == "2026-08-23T00:00:00Z"
    assert inventory.extract_capture_time("# Overview\n") is None
    units = inventory.extract_source_units("commands-p10:alpha", lower)
    assert any(unit["kind"] == "command-synopsis" for unit in units)


def test_source_unit_summaries_do_not_publish_example_payload_values() -> None:
    source = """# Resource

```
{"HostName": "private.example", "IPAddress": "192.0.2.1"}
```
"""

    units = inventory.extract_source_units("rest-p10:resource", source)

    assert units[0]["text"] == "payload-root:HostName"
    assert "private.example" not in json.dumps(units)
    assert "192.0.2.1" not in json.dumps(units)


def test_management_console_row_accounts_for_both_reference_snapshots() -> None:
    catalog = ROOT / "docs" / "capabilities"
    rows = json.loads((catalog / "rows.json").read_text())["rows"]
    row = next(row for row in rows if row["id"] == "rest:management-console")
    units = json.loads((catalog / "corpora.json").read_text())["source_units"]
    console_units = [
        unit
        for unit in units
        if unit["topic"]
        in {"rest-p10:management-console", "rest-p11:management-console"}
    ]

    assert len(console_units) == 6
    assert row["kind"] == "rest-operation"
    assert row["releases"] == ["power10", "power11"]
    assert row["parameters"] == []
    assert row["disposition"] == {"kind": "supported"}
    assert set(row["source_units"]) == {unit["id"] for unit in console_units}
    assert set(row["modes"]) == {
        unit["id"] for unit in console_units if unit["kind"] == "rest-resource"
    }
    assert all(
        unit["accounting"] == {"kind": "row", "id": row["id"]} for unit in console_units
    )


@pytest.mark.parametrize(
    ("operation", "row_ids"),
    [
        ("console.info", ["rest:management-console"]),
        ("console.list_resources", []),
        ("command.run", []),
    ],
)
def test_console_operations_bind_fixed_resources_or_explain_generic_dispatch(
    operation: str, row_ids: list[str]
) -> None:
    catalog = ROOT / "docs" / "capabilities" / "operations.json"
    operations = json.loads(catalog.read_text())["operations"]
    record = next(record for record in operations if record["operation"] == operation)

    assert record["row_ids"] == row_ids
    if row_ids:
        assert record["composite_reason"] is None
    else:
        assert isinstance(record["composite_reason"], str)
        assert record["composite_reason"].strip()


@pytest.mark.parametrize(
    ("operation", "job_names", "ownership"),
    [
        ("lpar.migrate", ["migrate", "migratevalidate"], True),
        ("lpar.migrate_affinity", ["migrate", "migratevalidate"], True),
        ("lpar.migrate_validate", ["migratevalidate"], False),
        ("lpar.migrate_abort", ["migrateabort"], True),
        ("lpar.migrate_recover", ["migraterecover"], True),
        ("lpar.remote_restart", ["remote_restart"], True),
    ],
)
def test_migration_operations_bind_reachable_jobs_and_support_reads(
    operation: str, job_names: list[str], ownership: bool
) -> None:
    catalog = ROOT / "docs" / "capabilities"
    operations = json.loads((catalog / "operations.json").read_text())["operations"]
    record = next(record for record in operations if record["operation"] == operation)
    expected = {
        "rest:managed-system",
        "rest:managed-system/logical-partition",
        "rest:jobs",
        "rest:job-status",
    }
    expected.update(
        "rest:jobs/managedsystem-jobs/logicalpartition_remoterestart-job"
        if job == "remote_restart"
        else f"rest:jobs/logicalpartition-jobs/{job}_logicalpartition-job"
        for job in job_names
    )
    if ownership:
        expected.add("cli:commands/lssyscfg")

    assert set(record["row_ids"]) == expected
    assert len(record["row_ids"]) == len(expected)
    assert record["composite_reason"] is None
    rows = {
        row["id"]: row
        for row in json.loads((catalog / "rows.json").read_text())["rows"]
    }
    units = {
        unit["id"]: unit
        for unit in json.loads((catalog / "corpora.json").read_text())["source_units"]
    }
    for row_id in expected:
        row = rows[row_id]
        assert row["source_units"]
        assert all(
            units[unit_id]["accounting"] == {"kind": "row", "id": row_id}
            for unit_id in row["source_units"]
        )


def test_sparse_maturity_allows_unknown_operations(tmp_path: Path) -> None:
    _minimal_inventory(tmp_path)

    report = inventory.validate_inventory(tmp_path, (), repo_root=tmp_path)

    assert not report.errors
    assert report.maturity_operation_count == 0


def test_maturity_rejects_extra_root_keys(tmp_path: Path) -> None:
    _minimal_inventory(tmp_path)
    maturity = inventory.load_json(tmp_path / "maturity.json")
    maturity["unexpected"] = None
    _write_json(tmp_path / "maturity.json", maturity)

    report = inventory.validate_inventory(tmp_path, (), repo_root=tmp_path)

    assert any("maturity.json: expected exactly" in error for error in report.errors)


def test_maturity_rejects_boolean_format_version(tmp_path: Path) -> None:
    _minimal_inventory(tmp_path)
    maturity = inventory.load_json(tmp_path / "maturity.json")
    maturity["format_version"] = True
    _write_json(tmp_path / "maturity.json", maturity)

    report = inventory.validate_inventory(tmp_path, (), repo_root=tmp_path)

    assert "maturity.json: format_version must be integer 4" in report.errors


def test_maturity_rejects_unknown_and_duplicate_operation_ids(tmp_path: Path) -> None:
    _minimal_inventory(tmp_path)
    _write_maturity(
        tmp_path, [_operation("unknown.operation"), _operation("unknown.operation")]
    )

    report = inventory.validate_inventory(tmp_path, (), repo_root=tmp_path)

    assert any("unknown operation" in error for error in report.errors)
    assert any("duplicate operation" in error for error in report.errors)


@pytest.mark.parametrize(
    ("state", "implemented", "missing", "expected"),
    [
        ("absent", [], [_scope()], False),
        ("partial", [_scope()], [_scope("missing")], False),
        ("implemented", [_scope()], [], False),
        ("absent", [_scope()], [_scope("missing")], True),
        ("partial", [_scope()], [], True),
        ("implemented", [_scope()], [_scope("missing")], True),
        ("partial", [_scope()], [_scope()], True),
    ],
)
def test_maturity_enforces_implementation_scope(
    tmp_path: Path,
    registered_inventory: tuple[inventory.RegistryTool, ...],
    state: str,
    implemented: list[dict[str, object]],
    missing: list[dict[str, object]],
    expected: bool,
) -> None:
    record = _operation(state=state)
    record["implementation"] = {
        "state": state,
        "implemented_scope": implemented,
        "missing_scope": missing,
    }
    report = _maturity_report(tmp_path, registered_inventory, [record])

    assert (
        any("contradictory implementation scope" in error for error in report.errors)
        is expected
    )
    if not expected:
        assert not report.errors


def test_sriov_catalog_separates_confirmation_from_mode_changes() -> None:
    catalog = inventory.load_json(ROOT / "docs" / "capabilities" / "maturity.json")
    record = next(
        row for row in catalog["operations"] if row["operation"] == "sriov.set_mode"
    )

    assert record["implementation"]["state"] == "partial"
    assert [
        scope["variant"] for scope in record["implementation"]["implemented_scope"]
    ] == ["current-mode-confirmation"]
    assert [
        scope["variant"] for scope in record["implementation"]["missing_scope"]
    ] == ["adapter-mode-transition"]
    assert record["evidence"] == []


@pytest.mark.parametrize("value", [[], {}, 1, None])
@pytest.mark.parametrize("field", ["operation", "state"])
def test_maturity_reports_invalid_identity_and_state_types(
    tmp_path: Path,
    registered_inventory: tuple[inventory.RegistryTool, ...],
    field: str,
    value: object,
) -> None:
    record = _operation()
    record["evidence"] = [_observation()]
    assert not _maturity_report(tmp_path, registered_inventory, [record]).errors

    if field == "state":
        record["implementation"][field] = value
        diagnostic = "invalid implementation state or scope lists"
    else:
        record[field] = value
        diagnostic = "operation must be a non-empty string"

    errors = _maturity_report(tmp_path, registered_inventory, [record]).errors

    assert any(
        error.startswith("maturity operation ") and diagnostic in error
        for error in errors
    )


def test_a_colliding_observation_id_is_distinguished_from_a_malformed_one(
    tmp_path: Path, registered_inventory: tuple[inventory.RegistryTool, ...]
) -> None:
    """Two defects, two diagnostics: a mis-cased id is not a collision."""
    record = _operation()
    record["evidence"] = [_observation(), _observation()]

    errors = _maturity_report(tmp_path, registered_inventory, [record]).errors

    assert (
        "maturity evidence st1-hmc-get-console-info: id must be catalog-wide unique"
        in errors
    )


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        ("id", [], "id must match [a-z0-9][a-z0-9-]*"),
        ("id", "Not-An-Id", "id must match [a-z0-9][a-z0-9-]*"),
        ("channel", [], "invalid channel or result"),
        ("channel", {}, "invalid channel or result"),
        ("result", [], "invalid channel or result"),
        ("result", "not-run", "invalid channel or result"),
        ("result", "skipped", "invalid channel or result"),
        ("cleanup", [], "invalid cleanup"),
        ("cleanup", {}, "invalid cleanup"),
        ("scenario", [], "scenario must match st<n>-<slug>"),
        ("scenario", "console-identity", "scenario must match st<n>-<slug>"),
    ],
)
def test_maturity_reports_invalid_observation_types(
    tmp_path: Path,
    registered_inventory: tuple[inventory.RegistryTool, ...],
    field: str,
    value: object,
    diagnostic: str,
) -> None:
    record = _operation()
    observation = _observation()
    record["evidence"] = [observation]
    assert not _maturity_report(tmp_path, registered_inventory, [record]).errors

    observation[field] = value
    errors = _maturity_report(tmp_path, registered_inventory, [record]).errors

    if field != "id":
        label = "maturity evidence st1-hmc-get-console-info"
    elif isinstance(value, str):
        label = f"maturity evidence {value}"
    else:
        label = "maturity operation system.list evidence"
    assert f"{label}: {diagnostic}" in errors


def test_maturity_format_one_is_rejected(
    tmp_path: Path, registered_inventory: tuple[inventory.RegistryTool, ...]
) -> None:
    """ADR 0127 supersedes 0126's evidence model; a format 1 catalog is not read."""
    _write_maturity(tmp_path, [])
    document = inventory.load_json(tmp_path / "maturity.json")
    document["format_version"] = 1
    _write_json(tmp_path / "maturity.json", document)

    report = inventory.validate_inventory(
        tmp_path, registered_inventory, repo_root=tmp_path
    )

    assert "maturity.json: format_version must be integer 4" in report.errors


@pytest.mark.parametrize(
    "mutation",
    [
        {"result": "not-run"},
        {"reason": "hardware unavailable"},
        {"prerequisites": ["a POWER10 frame"]},
        {"obligation": {"catalog": "console.info#st1"}},
    ],
)
def test_a_not_run_observation_is_rejected(
    tmp_path: Path,
    registered_inventory: tuple[inventory.RegistryTool, ...],
    mutation: dict[str, object],
) -> None:
    """Format 2 has one observation shape; the `not-run` placeholder is gone."""
    record = _operation()
    record["evidence"] = [{**_observation(), **mutation}]

    assert _maturity_report(tmp_path, registered_inventory, [record]).errors


@pytest.mark.parametrize(
    "mutation",
    [{"unexpected": True}, {"cleanup": None}],
)
def test_observation_key_sets_are_exact(
    tmp_path: Path,
    registered_inventory: tuple[inventory.RegistryTool, ...],
    mutation: dict[str, object],
) -> None:
    observation = {**_observation(), **mutation}
    if mutation.get("cleanup") is None:
        del observation["cleanup"]
    record = _operation()
    record["evidence"] = [observation]

    assert any(
        "expected exactly" in error
        for error in _maturity_report(tmp_path, registered_inventory, [record]).errors
    )


@pytest.mark.parametrize(
    ("field", "value", "accepted"),
    [
        ("hmc_release", "V10R3", True),
        ("hmc_release", "V10R3M1", True),
        ("hardware_family", "POWER10", True),
        # Every identifier class the repository's privacy rule names. A
        # `[A-Za-z0-9 ._-]{0,39}` class with an IPv4 rejection admits the first
        # four verbatim, which is why it was replaced rather than patched.
        ("hmc_release", "hmc01.lab.example.com", False),
        ("hmc_release", "0644C7T", False),
        ("hmc_release", "U78CB.001.WZS0044-P1-C2", False),
        ("hmc_release", "lab-hmc-3", False),
        ("hmc_release", "10.1.2.3", False),
        ("hardware_family", "hmc01.lab.example.com", False),
        ("hardware_family", "0644C7T", False),
        ("hardware_family", "U78CB.001.WZS0044-P1-C2", False),
        ("hardware_family", "lab-hmc-3", False),
        ("hardware_family", "10.1.2.3", False),
    ],
)
def test_environment_values_reject_private_identifiers(
    tmp_path: Path,
    registered_inventory: tuple[inventory.RegistryTool, ...],
    field: str,
    value: str,
    accepted: bool,
) -> None:
    record = _operation()
    record["evidence"] = [{**_observation(), field: value}]

    errors = _maturity_report(tmp_path, registered_inventory, [record]).errors

    assert (
        "maturity evidence st1-hmc-get-console-info: environment values do not "
        "match their grammar" in errors
    ) is not accepted


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("V1_0", True),
        ("V10_12", True),
        # Live HMCs report three components (ADR 0139).
        ("V1_17_0", True),
        ("V1_", False),
        ("(not set)", True),
        # Legacy only: the runner never writes it (ADR 0186).
        ("unrecorded", True),
        ("v1_0", False),
        ("V1_0 ", False),
        ("V1", False),
        ("", False),
        ("(NOT SET)", False),
        ("hmc01.lab.example.com", False),
        (None, False),
    ],
)
def test_schema_version_domain(
    tmp_path: Path,
    registered_inventory: tuple[inventory.RegistryTool, ...],
    value: object,
    accepted: bool,
) -> None:
    record = _operation()
    record["evidence"] = [{**_observation(), "schema_version": value}]

    errors = _maturity_report(tmp_path, registered_inventory, [record]).errors

    assert (
        "maturity evidence st1-hmc-get-console-info: schema_version must be "
        "V<n>_<n>[_<n>...], (not set) or unrecorded" in errors
    ) is not accepted


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        ({"tested_commit": "a" * 39}, "tested_commit must be a full SHA"),
        (
            {"closure_fingerprint": "b" * 63},
            "closure_fingerprint must be a full SHA-256",
        ),
        (
            {"assertions": ["Console UUID present"]},
            "assertions must be closed-shape tokens",
        ),
        ({"assertions": ["a-b", "a-b"]}, "assertions must be unique"),
        (
            {"result": "failed", "cleanup": "wiped"},
            "invalid cleanup",
        ),
        # What stops a hand-authored `passed` row that asserted nothing, or one
        # whose cleanup failed, from deriving `current`.
        ({"assertions": []}, "passed evidence requires assertions"),
        ({"cleanup": "failed"}, "a passed observation requires successful cleanup"),
    ],
)
def test_attempted_observation_fields_are_pattern_bound(
    tmp_path: Path,
    registered_inventory: tuple[inventory.RegistryTool, ...],
    mutation: dict[str, object],
    diagnostic: str,
) -> None:
    """`maturity.json` is hand-copied, so the validator is the only check it meets."""
    record = _operation()
    record["evidence"] = [{**_observation(), **mutation}]

    errors = _maturity_report(tmp_path, registered_inventory, [record]).errors

    assert f"maturity evidence st1-hmc-get-console-info: {diagnostic}" in errors


def _package(root: Path, modules: dict[str, str]) -> None:
    """Write a throwaway `src/hmcpctl/` package for the closure walk to read."""
    package = root / "src" / "hmcpctl"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name, source in modules.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def test_closure_fingerprint_changes_with_an_imported_module_only(
    tmp_path: Path,
) -> None:
    _package(
        tmp_path,
        {
            "a.py": "from .b import thing\n",
            "b.py": "thing = 1\n",
            "c.py": "other = 2\n",
        },
    )
    first = inventory.closure_fingerprint(tmp_path, "hmcpctl.a")

    (tmp_path / "src" / "hmcpctl" / "c.py").write_text("other = 3\n", encoding="utf-8")
    assert inventory.closure_fingerprint(tmp_path, "hmcpctl.a") == first

    (tmp_path / "src" / "hmcpctl" / "b.py").write_text("thing = 2\n", encoding="utf-8")
    assert inventory.closure_fingerprint(tmp_path, "hmcpctl.a") != first


@pytest.mark.parametrize(
    "statement",
    [
        "from .b import thing",
        "from . import b",
        "import hmcpctl.b",
        # A `TYPE_CHECKING` guard is a module-level `If`, so the import is absent
        # from `tree.body` itself; a `try:/except ImportError:` is the same shape.
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .b import thing",
        "try:\n    from .b import thing\nexcept ImportError:\n    thing = None",
        "try:\n    thing = None\nexcept ImportError:\n    from .b import thing",
        "try:\n    pass\nfinally:\n    from .b import thing",
    ],
)
def test_closure_covers_each_import_form(tmp_path: Path, statement: str) -> None:
    """`from . import b` has `module is None`; a walk reading only `module` misses it.

    The guarded forms pin the traversal depth from the other side: reading only
    `tree.body` skips them, and an operation whose only path to a module runs
    through a `TYPE_CHECKING` guard would then never go stale when it changes.
    """
    _package(tmp_path, {"a.py": f"{statement}\n", "b.py": "thing = 1\n"})

    paths = inventory.closure_paths(tmp_path, "hmcpctl.a")

    assert tmp_path / "src" / "hmcpctl" / "b.py" in paths


def test_closure_stops_at_a_function_boundary_inside_a_guard(tmp_path: Path) -> None:
    """The `If` recursion must not become an excuse to read function bodies."""
    _package(
        tmp_path,
        {
            "a.py": "if True:\n    def later():\n        from .b import thing\n",
            "b.py": "thing = 1\n",
        },
    )

    assert tmp_path / "src" / "hmcpctl" / "b.py" not in inventory.closure_paths(
        tmp_path, "hmcpctl.a"
    )


def test_closure_excludes_function_body_imports() -> None:
    """A deferred import is not part of the module's import-time implementation.

    `src/hmcpctl/__init__.py` imports `.cli` inside `main()` and sits on every
    resolution path, so an `ast.walk` implementation yields 179 of 180 files for
    every handler — ADR 0126's repository-wide fingerprint under another name.
    """
    paths = inventory.closure_paths(ROOT, "hmcpctl.server_tools.permissions")

    assert ROOT / "src" / "hmcpctl" / "cli.py" not in paths
    assert len(paths) < 20


def test_closure_resolves_packages() -> None:
    """`from ..jobs import JobOutcome` names a package and a class, not a module.

    A module-file-only walk finds neither file, so a change to
    `SUCCESSFUL_JOB_STATUSES` — the constant the job scenarios assert against —
    would leave their observations reading as current.
    """
    paths = inventory.closure_paths(ROOT, "hmcpctl.server_tools.jobs")

    assert ROOT / "src" / "hmcpctl" / "jobs" / "__init__.py" in paths
    assert ROOT / "src" / "hmcpctl" / "jobs" / "core.py" in paths


def test_lifecycle_closure_excludes_split_tool_modules() -> None:
    """LPAR tool modules retain ownership instead of re-exporting siblings."""
    paths = inventory.closure_paths(ROOT, "hmcpctl.server_tools.lpar.lifecycle")
    lpar = ROOT / "src" / "hmcpctl" / "server_tools" / "lpar"

    assert lpar / "lifecycle_boot.py" not in paths
    assert lpar / "lifecycle_create.py" not in paths


def test_lpar_get_closure_excludes_the_user_profile_builder() -> None:
    """Editing the UserProfile builder re-stales only the operations that build it (#1495).

    Every operation logs on, so the module holding the logon builder is in every
    closure; it shared `documents/access.py` with the UserProfile builder, and the
    package initializer re-exported every builder module besides.
    """
    paths = inventory.closure_paths(ROOT, "hmcpctl.server_tools.systems.core")
    documents = ROOT / "src" / "hmcpctl" / "documents"

    assert documents / "logon.py" in paths
    assert documents / "access.py" not in paths


def test_closure_containment(tmp_path: Path) -> None:
    """The walk never reads a symlink and never resolves above the package.

    A symlink pointing outside the repository would make the fingerprint
    machine-dependent, so the runner's recorded value and CI's recomputation
    could never agree and the observation would read stale forever.
    """
    _package(
        tmp_path,
        {
            "a.py": "from .b import thing\nfrom ....far import away\n",
            "outside.py": "thing = 1\n",
        },
    )
    package = tmp_path / "src" / "hmcpctl"
    (package / "b.py").symlink_to(package / "outside.py")

    paths = inventory.closure_paths(tmp_path, "hmcpctl.a")

    assert package / "b.py" not in paths
    assert paths == [package / "__init__.py", package / "a.py"]


_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _closure_registry(
    tmp_path: Path, operation: str = "system.list"
) -> tuple[inventory.RegistryTool, ...]:
    _package(tmp_path, {"a.py": "thing = 1\n"})
    return (
        inventory.RegistryTool(
            tool="hmc_list_systems",
            operation=operation,
            handler="hmcpctl.a.hmc_list_systems",
            signature="()",
            surfaces=("mcp",),
        ),
    )


@pytest.mark.parametrize(
    ("records", "expected", "reason"),
    [
        ([], "unrecorded", None),
        ([{"evidence": []}], "unevidenced", None),
        (
            [{"evidence": [{"closure_fingerprint": "c" * 64}]}],
            "stale",
            "closure-changed",
        ),
        (
            [{"evidence": [{"observed_at": "2026-01-01T00:00:00Z"}]}],
            "stale",
            "age-exceeded",
        ),
        ([{"evidence": [{"result": "failed"}]}], "failed", None),
        ([{"evidence": [{}]}], "current", None),
        # Only a live observation derives a state: the validator still admits
        # `contract-review` and `automated`, and promoting either as live
        # verification is exactly the claim ADR 0127 makes about `current`.
        ([{"evidence": [{"channel": "contract-review"}]}], "unevidenced", None),
        ([{"evidence": [{"channel": "automated"}]}], "unevidenced", None),
    ],
)
def test_derived_states(
    tmp_path: Path,
    records: list[dict[str, object]],
    expected: str,
    reason: str | None,
) -> None:
    """Staleness is derived when the catalog is read, never stored."""
    registry = _closure_registry(tmp_path)
    fingerprint = inventory.closure_fingerprint(tmp_path, "hmcpctl.a")
    catalog = [
        {
            "operation": "system.list",
            "implementation": _operation()["implementation"],
            "evidence": [
                {**_observation(fingerprint=fingerprint), **override}
                for override in record["evidence"]
            ],
        }
        for record in records
    ]

    states = inventory.derive_states(catalog, registry, tmp_path, _NOW)

    assert states["system.list"].state == expected
    assert states["system.list"].reason == reason


def test_report_line_carries_implementation_state(capsys) -> None:
    """A bare `current` would read as "live-verified" for a partial operation."""
    states = {
        "sriov.set_mode": inventory.OperationState("current", None, "partial"),
        "vnic.add": inventory.OperationState("unrecorded"),
    }

    assert inventory.verification_report(states, fail_on_stale=False) == 0

    output = capsys.readouterr().out
    assert "verification: sriov.set_mode partial current" in output
    assert "verification: vnic.add unrecorded" in output


def test_verification_report_summary_and_fail_on_stale(capsys) -> None:
    states = {
        "system.list": inventory.OperationState(
            "stale", "closure-changed", "implemented"
        ),
        "vnic.add": inventory.OperationState("unrecorded"),
    }

    assert inventory.verification_report(states, fail_on_stale=False) == 0
    assert (
        "verification coverage: 1 unrecorded, 0 unevidenced, 1 stale, 0 failed, "
        "0 current (of 2)" in capsys.readouterr().out
    )
    assert inventory.verification_report(states, fail_on_stale=True) == 1


def test_a_stale_observation_is_not_an_error(
    tmp_path: Path, registered_inventory: tuple[inventory.RegistryTool, ...]
) -> None:
    """Recording evidence must never turn the build red (ADR 0127)."""
    record = _operation()
    record["evidence"] = [_observation(fingerprint="c" * 64)]

    assert not _maturity_report(tmp_path, registered_inventory, [record]).errors


def test_verification_report_annotates_a_github_actions_run(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The CI job's whole purpose is the annotations, and nothing else covers them."""
    summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    states = {
        "system.list": inventory.OperationState("stale", "age-exceeded", "implemented"),
        "sriov.set_mode": inventory.OperationState("current", None, "partial"),
    }

    assert inventory.verification_report(states, fail_on_stale=False) == 0

    output = capsys.readouterr().out
    assert "::warning::system.list is stale: age-exceeded" in output
    assert "::warning::sriov.set_mode" not in output
    table = summary.read_text(encoding="utf-8")
    assert "| Operation | Implementation | State | Reason |" in table
    assert "| system.list | implemented | stale | age-exceeded |" in table
    assert "| sriov.set_mode | partial | current |  |" in table


def test_verification_report_survives_an_unwritable_step_summary(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """An unwritable summary path must not redden the pull request job."""
    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "missing" / "summary.md"))

    assert (
        inventory.verification_report(
            {"system.list": inventory.OperationState("unrecorded")}, fail_on_stale=False
        )
        == 0
    )
    assert "could not append the step summary" in capsys.readouterr().err


def test_verification_report_is_quiet_outside_github_actions(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    states = {
        "system.list": inventory.OperationState("stale", "age-exceeded", "implemented")
    }

    inventory.verification_report(states, fail_on_stale=False)

    assert "::warning::" not in capsys.readouterr().out


def test_closure_refuses_a_relative_import_that_leaves_the_package(
    tmp_path: Path,
) -> None:
    """At package depth 1, `from ..x import y` resolves against `src/` itself."""
    _package(tmp_path, {"a.py": "from ..outside import thing\n"})
    (tmp_path / "src" / "outside.py").write_text("thing = 1\n", encoding="utf-8")

    paths = inventory.closure_paths(tmp_path, "hmcpctl.a")

    assert tmp_path / "src" / "outside.py" not in paths
    assert paths == [
        tmp_path / "src" / "hmcpctl" / "__init__.py",
        tmp_path / "src" / "hmcpctl" / "a.py",
    ]


def test_fail_on_stale_requires_the_report(capsys) -> None:
    """Silently ignoring the flag would report success on a stale catalog."""
    with pytest.raises(SystemExit) as raised:
        inventory.main(["--fail-on-stale"])

    assert raised.value.code == 2
    assert "--fail-on-stale requires --verification-report" in capsys.readouterr().err


def test_runtime_projection_renders_only_recorded_operations(tmp_path: Path) -> None:
    registry = _closure_registry(tmp_path)
    records = [_operation(), _operation("sriov.set_mode", state="partial")]

    rendered = inventory.render_runtime_projection(records, registry, {})

    assert rendered.endswith("\n")
    assert json.loads(rendered) == {
        "format_version": 1,
        "runtime_eligibility": "existing-runtime-guards",
        "operations": [
            {
                "operation": "system.list",
                "implementation": "implemented",
                "verification": "unevidenced",
                "reason": None,
                "observed_at": None,
            }
        ],
    }


def test_runtime_projection_preserves_latest_live_observation_time(
    tmp_path: Path,
) -> None:
    registry = _closure_registry(tmp_path)
    fingerprint = inventory.closure_fingerprint(tmp_path, "hmcpctl.a")
    record = _operation()
    record["evidence"] = [
        _observation(
            identity="st1-older",
            fingerprint=fingerprint,
            observed_at="2026-08-01T12:00:00Z",
        ),
        _observation(
            identity="st1-newer",
            result="failed",
            fingerprint=fingerprint,
            observed_at="2026-09-01T12:00:00Z",
        ),
    ]

    rendered = inventory.render_release_verification([record], registry, tmp_path, _NOW)

    projected = json.loads(rendered)["operations"][0]
    assert projected["verification"] == "failed"
    assert projected["observed_at"] == "2026-09-01T12:00:00Z"


def test_release_snapshot_derivation_does_not_apply_age_expiry(
    tmp_path: Path,
) -> None:
    registry = _closure_registry(tmp_path)
    fingerprint = inventory.closure_fingerprint(tmp_path, "hmcpctl.a")
    record = _operation()
    record["evidence"] = [
        _observation(
            fingerprint=fingerprint,
            observed_at="2026-01-01T12:00:00Z",
        )
    ]

    rendered = inventory.render_release_verification([record], registry, tmp_path, _NOW)

    projected = json.loads(rendered)["operations"][0]
    assert projected["verification"] == "current"
    assert projected["reason"] is None


def test_write_runtime_projection_is_atomic(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "projection.json"
    path.write_text("old\n", encoding="utf-8")

    def fail_replace(_temporary, _path):
        raise OSError("replacement failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="replacement failed"):
        inventory.write_runtime_projection(path, "new\n")

    assert path.read_text(encoding="utf-8") == "old\n"
    assert not list(tmp_path.glob(".projection.json.*.tmp"))


@pytest.mark.parametrize("contents", [None, "not json\n", "{}\n"])
def test_default_gate_rejects_missing_malformed_and_drifted_projection(
    tmp_path: Path, monkeypatch, capsys, contents: str | None
) -> None:
    inventory_root = tmp_path / "inventory"
    _minimal_inventory(inventory_root)
    projection = tmp_path / "_operation_maturity.json"
    if contents is not None:
        projection.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(inventory, "DEFAULT_RUNTIME_PROJECTION", projection)
    monkeypatch.setattr(inventory, "discover_registry", tuple)

    assert inventory.main(["--inventory", str(inventory_root)]) == 1
    assert "just capability-metadata" in capsys.readouterr().err


def test_write_runtime_projection_option_generates_requested_path(
    tmp_path: Path, monkeypatch
) -> None:
    inventory_root = tmp_path / "inventory"
    _minimal_inventory(inventory_root)
    projection = tmp_path / "projection.json"
    monkeypatch.setattr(inventory, "discover_registry", tuple)

    assert (
        inventory.main(
            [
                "--inventory",
                str(inventory_root),
                "--write-runtime-projection",
                str(projection),
            ]
        )
        == 0
    )

    assert projection.read_text(
        encoding="utf-8"
    ) == inventory.render_runtime_projection([], (), {})


def test_lpar_modify_binds_assignment_delegate_rows():
    catalog = json.loads((ROOT / "docs/capabilities/operations.json").read_text())
    operation = next(
        row for row in catalog["operations"] if row["operation"] == "lpar.modify"
    )
    assert set(operation["row_ids"]) == {
        "cli:commands/chhwres",
        "cli:commands/chsyscfg",
        "cli:commands/lshmc",
        "cli:commands/lshwres",
        "cli:commands/lssyscfg",
        "rest:managed-system/logical-partition",
    }


@pytest.fixture
def shared_evidence(tmp_path):
    registry = _closure_registry(tmp_path)
    registry += (replace(registry[0], tool="hmc_other", operation="system.other"),)
    fingerprint = inventory.closure_fingerprint(tmp_path, "hmcpctl.a")
    records = [_operation(tool.operation) for tool in registry]
    for record in records:
        record["evidence"] = [_observation(fingerprint=fingerprint)]
    return records, registry


def test_shared_handlers_are_fingerprinted_once(tmp_path, monkeypatch, shared_evidence):
    records, registry = shared_evidence
    fingerprint = Mock(wraps=inventory.closure_fingerprint)
    monkeypatch.setattr(inventory, "closure_fingerprint", fingerprint)

    states = inventory.derive_states(records, registry, tmp_path, _NOW)

    assert states == {
        tool.operation: inventory.OperationState(
            "current", None, "implemented", "2026-09-06T12:00:00Z"
        )
        for tool in registry
    }
    fingerprint.assert_called_once_with(tmp_path, "hmcpctl.a")


@pytest.mark.parametrize("change", ["source", "import", "dependency", "missing"])
def test_new_derivation_observes_source_changes(tmp_path, shared_evidence, change):
    records, registry = shared_evidence
    module = tmp_path / "src/hmcpctl/a.py"
    dependency = tmp_path / "src/hmcpctl/b.py"
    if change == "dependency":
        module.write_text("from .b import value\n", encoding="utf-8")
        dependency.write_text("value = 1\n", encoding="utf-8")
        fingerprint = inventory.closure_fingerprint(tmp_path, "hmcpctl.a")
        for record in records:
            record["evidence"][0]["closure_fingerprint"] = fingerprint
    assert (
        inventory.derive_states(records, registry, tmp_path, _NOW)["system.list"].state
        == "current"
    )

    if change == "source":
        module.write_text("thing = 2\n", encoding="utf-8")
    elif change == "import":
        module.write_text("from .b import value\n", encoding="utf-8")
        dependency.write_text("value = 1\n", encoding="utf-8")
    elif change == "dependency":
        dependency.write_text("value = 2\n", encoding="utf-8")
    else:
        module.unlink()

    states = inventory.derive_states(records, registry, tmp_path, _NOW)
    assert {state.reason for state in states.values()} == {"closure-changed"}
    if change == "missing":
        assert inventory.closure_paths(tmp_path, "hmcpctl.a") == [
            tmp_path / "src/hmcpctl/__init__.py"
        ]


def test_new_derivation_isolated_by_root(tmp_path, shared_evidence):
    records, registry = shared_evidence
    other = tmp_path / "other"
    _package(other, {"a.py": "thing = 2\n"})

    fingerprints = {}
    first = inventory.derive_states(
        records, registry, tmp_path, _NOW, _fingerprints=fingerprints
    )
    second = inventory.derive_states(
        records, registry, other, _NOW, _fingerprints=fingerprints
    )
    again = inventory.derive_states(
        records, registry, tmp_path, _NOW, _fingerprints=fingerprints
    )

    assert {state.state for state in first.values()} == {"current"}
    assert {state.reason for state in second.values()} == {"closure-changed"}
    assert again == first


def test_derivation_preserves_read_failure(tmp_path, monkeypatch, shared_evidence):
    records, registry = shared_evidence
    read_bytes = Path.read_bytes
    module = tmp_path / "src/hmcpctl/a.py"

    def unreadable(path):
        if path == module:
            raise PermissionError("fixture read denied")
        return read_bytes(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", unreadable)
        with pytest.raises(PermissionError, match="fixture read denied"):
            inventory.derive_states(records, registry, tmp_path, _NOW)
    assert (
        inventory.derive_states(records, registry, tmp_path, _NOW)["system.list"].state
        == "current"
    )


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    ("other", "expected"),
    [
        # Two producers in one scenario record the same second; the greater id
        # decides, as in promotion, whichever row the catalog lists first.
        ({"identity": "st1-hmc-get-console-info-b"}, "failed"),
        ({"identity": "st1-hmc-a-console"}, "current"),
        # A newer retained row decides whatever its result.
        (
            {"identity": "st1-hmc-other", "observed_at": "2026-09-06T12:00:01Z"},
            "failed",
        ),
    ],
)
def test_newest_live_row_by_time_then_id_decides_state(
    tmp_path, shared_evidence, other, expected, reverse
):
    records, registry = shared_evidence
    passed = records[0]["evidence"][0]
    failed = _observation(result="failed", **other)
    failed["closure_fingerprint"] = passed["closure_fingerprint"]
    records[0]["evidence"] = [failed, passed] if reverse else [passed, failed]

    state = inventory.derive_states(records, registry, tmp_path, _NOW)
    assert state[records[0]["operation"]].state == expected


@pytest.mark.parametrize("fail_on_stale", [False, True])
def test_report_reuses_fingerprints_and_preserves_outputs(
    tmp_path, monkeypatch, capsys, registered_inventory, fail_on_stale
):
    registry = registered_inventory
    module_name = registry[0].handler.rsplit(".", 1)[0]
    relative_module = module_name.removeprefix("hmcpctl.").replace(".", "/") + ".py"
    _package(tmp_path, {relative_module: "value = 1\n"})
    fingerprint = inventory.closure_fingerprint(tmp_path, module_name)
    record = _operation()
    record["evidence"] = [
        _observation(fingerprint=fingerprint, observed_at="2026-01-01T12:00:00Z")
    ]
    _write_maturity(tmp_path, [record])
    projection = tmp_path / "projection.json"
    snapshot_path = tmp_path / "release-verification.json"
    snapshot_path.write_text(
        inventory.render_release_verification([record], registry, tmp_path, _NOW)
    )
    expected = inventory.render_runtime_projection(
        [record], registry, inventory.load_release_verification(snapshot_path)
    )
    projection.write_text(expected, encoding="utf-8")
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    monkeypatch.setattr(inventory, "DEFAULT_RUNTIME_PROJECTION", projection)
    monkeypatch.setattr(inventory, "discover_registry", lambda: registry)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    counted = Mock(wraps=inventory.closure_fingerprint)
    monkeypatch.setattr(inventory, "closure_fingerprint", counted)
    args = ["--inventory", str(tmp_path), "--verification-report"]
    if fail_on_stale:
        args.append("--fail-on-stale")

    assert inventory.main(args) == int(fail_on_stale)
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == (
        "verification: system.list implemented stale\n"
        "verification coverage: 0 unrecorded, 0 unevidenced, 1 stale, "
        "0 failed, 0 current (of 1)\n"
    )
    assert projection.read_bytes() == expected.encode()
    counted.assert_called_once_with(tmp_path, module_name)

    (tmp_path / "src/hmcpctl" / relative_module).write_text(
        "value = 2\n", encoding="utf-8"
    )
    counted.reset_mock()
    assert inventory.main(args) == int(fail_on_stale)
    output = capsys.readouterr()
    assert "verification: system.list implemented stale" in output.out
    assert output.err == ""
    assert projection.read_bytes() == expected.encode()
    counted.assert_called_once_with(tmp_path, module_name)


def _snapshot_row(operation="system.list", verification="current"):
    return {
        "operation": operation,
        "verification": verification,
        "reason": None,
        "observed_at": "2026-01-01T12:00:00Z"
        if verification != "unevidenced"
        else None,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=True),
        lambda d: d.update(format_version=True),
        lambda d: d.update(operations={}),
        lambda d: d["operations"].append(None),
        lambda d: d["operations"][0].update(implementation="implemented"),
        lambda d: d["operations"][0].update(operation="Bad.read"),
        lambda d: d["operations"][0].update(verification=[]),
        lambda d: d["operations"][0].update(reason=[]),
        lambda d: d["operations"][0].update(reason="age-exceeded"),
        lambda d: d["operations"][0].update(verification="stale"),
        lambda d: d["operations"][0].update(observed_at=None),
        lambda d: d["operations"][0].update(observed_at="2026-02-30T00:00:00Z"),
        lambda d: d["operations"][0].update(observed_at="2026-01-01T00:00:00+00:00"),
        lambda d: d["operations"][0].update(verification="unevidenced"),
        lambda d: d["operations"].append(d["operations"][0].copy()),
        lambda d: d["operations"].append(_snapshot_row("alpha.read")),
    ],
)
def test_release_snapshot_rejects_invalid_schema(tmp_path, mutate):
    path = tmp_path / "release-verification.json"
    document = {"format_version": 1, "operations": [_snapshot_row()]}
    mutate(document)
    _write_json(path, document)
    with pytest.raises(inventory.InventoryError):
        inventory.load_release_verification(path)


@pytest.mark.parametrize(
    "content",
    [
        b'{"format_version":1,"format_version":1}',
        b"[]",
        b"\xff",
        b"{",
        b" " * (1_048_576 + 1),
    ],
    ids=["duplicate", "array", "unicode", "json", "oversize"],
)
def test_release_snapshot_rejects_invalid_bytes(tmp_path, content):
    path = tmp_path / "release-verification.json"
    path.write_bytes(content)
    with pytest.raises(inventory.InventoryError):
        inventory.load_release_verification(path)


def test_release_snapshot_missing_file_fails(tmp_path):
    with pytest.raises(inventory.InventoryError):
        inventory.load_release_verification(tmp_path / "missing.json")


def test_release_snapshot_accepts_frozen_states(tmp_path):
    rows = [
        _snapshot_row("alpha.read", "unevidenced"),
        _snapshot_row("beta.read", "failed"),
        _snapshot_row("gamma.read", "stale"),
        _snapshot_row(),
    ]
    rows[2]["reason"] = "closure-changed"
    path = tmp_path / "release-verification.json"
    _write_json(path, {"format_version": 1, "operations": rows})
    assert inventory.load_release_verification(path) == {
        r["operation"]: r for r in rows
    }


def test_projection_freezes_snapshot_fields_and_preserves_catalog_authority(tmp_path):
    registry = _closure_registry(tmp_path)
    record = _operation()
    snapshot = {"system.list": _snapshot_row()}
    frozen = inventory.render_runtime_projection([record], registry, snapshot)
    (tmp_path / "src/hmcpctl/a.py").write_text("value = 999\n")
    record["evidence"] = [_observation(result="failed")]
    assert inventory.render_runtime_projection([record], registry, snapshot) == frozen
    record["implementation"]["state"] = "partial"
    changed = json.loads(
        inventory.render_runtime_projection([record], registry, snapshot)
    )
    assert changed["operations"][0]["implementation"] == "partial"
    assert changed["operations"][0]["verification"] == "current"
    assert (
        json.loads(inventory.render_runtime_projection([], registry, snapshot))[
            "operations"
        ]
        == []
    )
    missing = json.loads(inventory.render_runtime_projection([record], registry, {}))[
        "operations"
    ][0]
    assert (missing["verification"], missing["reason"], missing["observed_at"]) == (
        "unevidenced",
        None,
        None,
    )


def test_release_mode_reports_closure_drift_without_changing_ordinary_projection(
    tmp_path, monkeypatch, capsys, registered_inventory
):
    registry = registered_inventory
    module = registry[0].handler.rsplit(".", 1)[0]
    relative = module.removeprefix("hmcpctl.").replace(".", "/") + ".py"
    _package(tmp_path, {relative: "value = 1\n"})
    record = _operation()
    record["evidence"] = [
        _observation(
            fingerprint=inventory.closure_fingerprint(tmp_path, module),
            observed_at="2026-01-01T12:00:00Z",
        )
    ]
    _write_maturity(tmp_path, [record])
    snapshot = tmp_path / "release-verification.json"
    snapshot.write_text(
        inventory.render_release_verification([record], registry, tmp_path, _NOW)
    )
    projection = tmp_path / "projection.json"
    projection.write_text(
        inventory.render_runtime_projection(
            [record], registry, inventory.load_release_verification(snapshot)
        )
    )
    expected = projection.read_bytes()
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    monkeypatch.setattr(inventory, "DEFAULT_RUNTIME_PROJECTION", projection)
    monkeypatch.setattr(inventory, "discover_registry", lambda: registry)
    args = ["--inventory", str(tmp_path)]
    assert inventory.main([*args, "--release"]) == 0
    (tmp_path / "src/hmcpctl" / relative).write_text("value = 2\n")
    assert inventory.main(args) == 0
    assert projection.read_bytes() == expected
    assert inventory.main([*args, "--release"]) == 1
    assert "release verification snapshot differs" in capsys.readouterr().err
    assert (
        inventory.main(
            [*args, "--release", "--write-runtime-projection", str(projection)]
        )
        == 1
    )
    assert projection.read_bytes() == expected
    record["implementation"]["state"] = "partial"
    record["implementation"]["missing_scope"] = [_scope("other")]
    _write_maturity(tmp_path, [record])
    assert inventory.main(args) == 1
    assert "just capability-metadata" in capsys.readouterr().err


def test_missing_snapshot_refuses_regeneration(tmp_path, monkeypatch, capsys):
    _minimal_inventory(tmp_path)
    (tmp_path / "release-verification.json").unlink()
    projection = tmp_path / "projection.json"
    projection.write_text("previous\n")
    monkeypatch.setattr(inventory, "discover_registry", tuple)
    assert (
        inventory.main(
            [
                "--inventory",
                str(tmp_path),
                "--write-runtime-projection",
                str(projection),
            ]
        )
        == 1
    )
    assert "cannot read release verification snapshot" in capsys.readouterr().err
    assert projection.read_text() == "previous\n"


def _release_document():
    return {
        "format_version": 1,
        "operations": [
            {
                "operation": "system.get",
                "arm": "round2",
                "scenario": "st1-system-inventory",
            }
        ],
    }


def test_release_claims_accept_known_relationship_without_observations(tmp_path):
    path = tmp_path / "v0.2.0.json"
    document = _release_document()
    _write_json(path, document)
    assert inventory.load_release_claims(path, {"system.get"}) == document["operations"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation", "system.unknown"),
        ("arm", "unknown"),
        ("scenario", "st1-unknown"),
        ("arm", "dedicated"),
        ("scenario", "st1-lpar-inventory"),
        ("operation", "lpar.get"),
        ("arm", []),
        ("scenario", None),
        ("operation", ""),
    ],
)
def test_release_claims_reject_unknown_or_crossed_references(tmp_path, field, value):
    document = _release_document()
    document["operations"][0][field] = value
    path = tmp_path / "v0.2.0.json"
    _write_json(path, document)
    with pytest.raises(inventory.InventoryError):
        inventory.load_release_claims(path, {"system.get", "lpar.get"})


@pytest.mark.parametrize(
    "document",
    [
        [],
        {},
        {"format_version": True, "operations": []},
        {"format_version": 2, "operations": []},
        {"format_version": 1, "operations": None},
        {"format_version": 1, "operations": [], "extra": 1},
        {"format_version": 1, "operations": [None]},
        {"format_version": 1, "operations": [{}]},
    ],
)
def test_release_claims_reject_malformed_shape(tmp_path, document):
    path = tmp_path / "v0.2.0.json"
    _write_json(path, document)
    with pytest.raises(inventory.InventoryError):
        inventory.load_release_claims(path, {"system.get"})


def test_release_claims_reject_duplicate_operations(tmp_path):
    document = _release_document()
    document["operations"] *= 2
    path = tmp_path / "v0.2.0.json"
    _write_json(path, document)
    with pytest.raises(inventory.InventoryError, match="duplicate"):
        inventory.load_release_claims(path, {"system.get"})


@pytest.mark.parametrize(
    "text", ["{", '{"format_version":1,"format_version":1,"operations":[]}']
)
def test_release_claims_reject_invalid_json(tmp_path, text):
    path = tmp_path / "v0.2.0.json"
    path.write_text(text)
    with pytest.raises(inventory.InventoryError):
        inventory.load_release_claims(path, set())


def test_inventory_validates_each_retained_release(tmp_path):
    _minimal_inventory(tmp_path)
    releases = tmp_path / "releases"
    releases.mkdir(exist_ok=True)
    _write_json(releases / "v0.2.0.json", _release_document())
    report = inventory.validate_inventory(tmp_path, [])
    assert any("unknown claimed operation" in error for error in report.errors)


def test_inventory_requires_retained_release_files(tmp_path):
    _minimal_inventory(tmp_path)
    releases = tmp_path / "releases"
    if releases.exists():
        for path in releases.iterdir():
            path.unlink()
    assert any(
        "release claims" in error
        for error in inventory.validate_inventory(tmp_path, []).errors
    )


def test_initial_release_claims_match_v010_scope():
    expected = {
        "system.get": ("round2", "st1-system-inventory"),
        "lpar.get": ("round2", "st1-lpar-inventory"),
        "lpar.summary": ("round2", "st1-lpar-inventory"),
        "lpar.get_state": ("bare-cec", "st35-bare-cec"),
        "job.get": ("bare-cec", "st35-bare-cec"),
        "job.wait": ("bare-cec", "st35-bare-cec"),
        "lpar.list_refcodes": ("bare-cec", "st35-bare-cec"),
        "lpar.create": ("lpar-power", "st41-lpar-power"),
        "lpar.power_on": ("lpar-power", "st41-lpar-power"),
        "lpar.power_off": ("lpar-power", "st41-lpar-power"),
        "lpar.capture_console": ("lpar-power", "st41-lpar-power"),
        "lpar.delete": ("lpar-power", "st41-lpar-power"),
        "pcie.list_dedicated_slots": ("dedicated", "st29-pcie-inventory"),
        "pcie.assign_dedicated_slot": ("dedicated", "st29-dedicated-pcie"),
        "pcie.unassign_dedicated_slot": ("dedicated", "st36-io-slots"),
    }
    claims = inventory.load_release_claims(
        ROOT / "docs/capabilities/releases/v0.1.0.json", set(expected)
    )
    assert {
        row["operation"]: (row["arm"], row["scenario"]) for row in claims
    } == expected
    assert len(claims) == 15


@pytest.mark.parametrize("name", ["release.json", "v1_0.json", "v1.0.txt"])
def test_release_claims_reject_invalid_filename(tmp_path, name):
    path = tmp_path / name
    _write_json(path, _release_document())
    with pytest.raises(inventory.InventoryError, match="filename"):
        inventory.load_release_claims(path, {"system.get"})


def test_release_claims_missing_file_is_inventory_error(tmp_path):
    with pytest.raises(inventory.InventoryError):
        inventory.load_release_claims(tmp_path / "v1.0.json", {"system.get"})


def test_inventory_requires_release_directory(tmp_path):
    _minimal_inventory(tmp_path)
    (tmp_path / "releases/v0.0.0.json").unlink()
    (tmp_path / "releases").rmdir()
    assert any(
        "cannot read release claims" in error
        for error in inventory.validate_inventory(tmp_path, []).errors
    )


def test_release_claims_reject_bare_cec_in_mutually_exclusive_all_arm(tmp_path):
    path = tmp_path / "v0.2.0.json"
    _write_json(
        path,
        {
            "format_version": 1,
            "operations": [
                {"operation": "job.get", "arm": "all", "scenario": "st35-bare-cec"}
            ],
        },
    )
    with pytest.raises(inventory.InventoryError, match="unknown release claim route"):
        inventory.load_release_claims(path, {"job.get"})


@pytest.mark.parametrize(
    "state", ["unrecorded", "unevidenced", "stale", "failed", "current"]
)
def test_claimed_report_states(state, capsys):
    states = {
        "system.get": inventory.OperationState(
            state, "closure-changed" if state == "stale" else None
        )
    }
    assert inventory.verification_report(
        states, fail_on_stale=False, require_current=True
    ) == int(state != "current")
    output = capsys.readouterr()
    assert f"verification: system.get {state}" in output.out
    if state != "current":
        assert f"ERROR: system.get: {state}" in output.err
    if state == "stale":
        assert "closure-changed" in output.err


def test_claimed_report_names_every_failure(capsys):
    states = {
        name: inventory.OperationState("unevidenced")
        for name in ("system.get", "lpar.get")
    }
    assert (
        inventory.verification_report(states, fail_on_stale=False, require_current=True)
        == 1
    )
    assert capsys.readouterr().err.splitlines() == [
        "ERROR: lpar.get: unevidenced",
        "ERROR: system.get: unevidenced",
    ]
    assert (
        inventory.verification_report({}, fail_on_stale=False, require_current=True)
        == 0
    )


@pytest.fixture
def claimed_gate(tmp_path, monkeypatch):
    _minimal_inventory(tmp_path)
    registry = tuple(
        tool
        for tool in inventory.discover_registry()
        if tool.operation in {"system.get", "system.list"}
    )
    _write_json(
        tmp_path / "operations.json",
        {
            "format_version": 1,
            "operations": [
                {
                    "tool": tool.tool,
                    "operation": tool.operation,
                    "handler": tool.handler,
                    "signature": tool.signature,
                    "surfaces": list(tool.surfaces),
                    "row_ids": [],
                    "composite_reason": "anonymous fixture operation",
                    "tests": [],
                }
                for tool in registry
            ],
        },
    )
    modules = {tool.handler.rsplit(".", 1)[0] for tool in registry}
    _package(
        tmp_path,
        {
            module.removeprefix("hmcpctl.").replace(".", "/") + ".py": "value = 1\n"
            for module in modules
        },
    )
    records = []
    for tool in registry:
        record = _operation(tool.operation)
        record["evidence"] = [
            _observation(
                identity="st1-" + tool.operation.replace(".", "-"),
                fingerprint=inventory.closure_fingerprint(
                    tmp_path, tool.handler.rsplit(".", 1)[0]
                ),
                observed_at="2026-09-06T12:00:00Z"
                if tool.operation == "system.get"
                else "2026-01-01T12:00:00Z",
            )
        ]
        records.append(record)
    _write_maturity(tmp_path, records)
    snapshot = tmp_path / "release-verification.json"
    snapshot.write_text(
        inventory.render_release_verification(records, registry, tmp_path, _NOW)
    )
    projection = tmp_path / "projection.json"
    projection.write_text(
        inventory.render_runtime_projection(
            records, registry, inventory.load_release_verification(snapshot)
        )
    )
    claims = tmp_path / "v0.2.0.json"
    _write_json(claims, _release_document())
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    monkeypatch.setattr(inventory, "DEFAULT_RUNTIME_PROJECTION", projection)
    monkeypatch.setattr(inventory, "discover_registry", lambda: registry)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return ["--inventory", str(tmp_path), "--require-current", str(claims)]


@pytest.mark.parametrize(
    "days,microseconds,expected", [(0, 0, 0), (90, 0, 0), (90, 1, 1), (-1, 0, 0)]
)
def test_claimed_gate_age_boundary(
    claimed_gate, tmp_path, monkeypatch, capsys, days, microseconds, expected
):
    from datetime import timedelta

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _NOW + timedelta(days=days, microseconds=microseconds)

    monkeypatch.setattr(inventory, "datetime", Clock)
    projection = tmp_path / "projection.json"
    before = projection.read_bytes()
    assert inventory.main(claimed_gate) == expected
    output = capsys.readouterr()
    assert "system.get" in output.out
    assert "system.list" not in output.out
    assert "(of 1)" in output.out
    if expected:
        assert "ERROR: system.get: stale (age-exceeded)" in output.err
    assert projection.read_bytes() == before


@pytest.mark.parametrize(
    "change,expected",
    [
        ("closure", 1),
        ("failed", 1),
        ("unevidenced", 1),
        ("unrecorded", 1),
        ("empty", 0),
        ("report", 0),
        ("release", 0),
    ],
)
def test_claimed_gate_main(
    claimed_gate, tmp_path, monkeypatch, capsys, change, expected
):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _NOW

    monkeypatch.setattr(inventory, "datetime", Clock)
    if change == "closure":
        module = next(
            tool.handler.rsplit(".", 1)[0]
            for tool in inventory.discover_registry()
            if tool.operation == "system.get"
        )
        source = tmp_path / "src" / Path(*module.split(".")).with_suffix(".py")
        source.write_text("value = 2\n")
    elif change in {"failed", "unevidenced", "unrecorded"}:
        records = inventory._maturity_records(tmp_path)
        record = next(
            record for record in records if record["operation"] == "system.get"
        )
        if change == "failed":
            record["evidence"][0]["result"] = "failed"
        elif change == "unevidenced":
            record["evidence"] = []
        else:
            records.remove(record)
        _write_maturity(tmp_path, records)
        projection = inventory.render_runtime_projection(
            records,
            inventory.discover_registry(),
            inventory.load_release_verification(tmp_path / "release-verification.json"),
        )
        (tmp_path / "projection.json").write_text(projection)
    elif change == "empty":
        _write_json(tmp_path / "v0.2.0.json", {"format_version": 1, "operations": []})
    else:
        claimed_gate.append(
            "--verification-report" if change == "report" else "--release"
        )
    assert inventory.main(claimed_gate) == expected
    output = capsys.readouterr()
    if expected:
        assert "ERROR: system.get:" in output.err
        assert (
            "closure-changed" in output.err
            if change == "closure"
            else change in output.err
        )
    if change == "empty":
        assert "(of 0)" in output.out


@pytest.mark.parametrize("problem", ["missing", "json", "unknown", "duplicate"])
def test_claimed_gate_input_errors(claimed_gate, tmp_path, capsys, problem):
    path = tmp_path / "v0.2.0.json"
    if problem == "missing":
        path.unlink()
    elif problem == "json":
        path.write_text("{broken")
    else:
        document = _release_document()
        if problem == "unknown":
            document["operations"][0]["operation"] = "unknown.operation"
        else:
            document["operations"] *= 2
        _write_json(path, document)
    assert inventory.main(claimed_gate) == 1
    output = capsys.readouterr()
    assert "ERROR:" in output.err
    assert "verification coverage:" not in output.out


@pytest.mark.parametrize("extra", [[], ["--verification-report"]])
def test_claimed_gate_usage(extra, capsys):
    with pytest.raises(SystemExit) as error:
        inventory.main(["--require-current", "v0.2.0.json", "--fail-on-stale", *extra])
    assert error.value.code == 2
    assert (
        "cannot combine --require-current with --fail-on-stale"
        in capsys.readouterr().err
    )
