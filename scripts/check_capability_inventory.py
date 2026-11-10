"""Validate the versioned HMC reference capability inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from capability_evidence import registry as capability_registry
from capability_evidence import schema as capability_schema
from capability_evidence import verification as capability_verification
from capability_evidence.registry import RegistryTool
from capability_evidence.schema import InventoryError
from capability_evidence.verification import OperationState

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "docs" / "capabilities"
DEFAULT_RUNTIME_PROJECTION = ROOT / "src" / "hmcpctl" / "_operation_maturity.json"
HEX_256 = re.compile(r"(?:[0-9a-f]{8}-){7}[0-9a-f]{8}")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")
TABLE_SEPARATOR = re.compile(r"^\|(?:\s*:?-+:?\s*\|)+$")
CAPTURE = re.compile(r"^(?:captured|Captured):\s*(\S.*)$", re.MULTILINE)
METHODS = {"GET", "POST", "PUT", "DELETE"}
DISPOSITIONS = {"supported", "coverage-child", "proposed-exclusion", "unknown"}


@dataclass(frozen=True)
class Report:
    errors: tuple[str, ...]
    unknown_ids: tuple[str, ...]
    proposed_exclusion_ids: tuple[str, ...]
    pending_ids: tuple[str, ...]
    topic_count: int
    source_unit_count: int
    row_count: int
    operation_count: int
    maturity_operation_count: int

    @property
    def complete(self) -> bool:
        return not (
            self.errors
            or self.unknown_ids
            or self.proposed_exclusion_ids
            or self.pending_ids
        )


def extract_capture_time(text: str) -> str | None:
    match = CAPTURE.search(text)
    return match.group(1).strip() if match else None


def format_sha256(value: bytes) -> str:
    digest = hashlib.sha256(value).hexdigest()
    return "-".join(digest[index : index + 8] for index in range(0, 64, 8))


def _unit(topic_id: str, kind: str, line: int, text: str) -> dict[str, object]:
    normalized = " ".join(text.split())
    return {
        "id": f"{topic_id}:L{line:05d}:{kind}",
        "topic": topic_id,
        "kind": kind,
        "line": line,
        "sha256": format_sha256(normalized.encode()),
        "text": _safe_summary(kind, normalized),
    }


def _safe_summary(kind: str, text: str) -> str:
    """Keep structural source evidence without publishing example identifiers."""
    if kind == "command-synopsis":
        tokens = re.findall(r"(?<!\w)--?[A-Za-z0-9][A-Za-z0-9_-]*|^[A-Za-z0-9_]+", text)
        return " ".join(dict.fromkeys(tokens)) or "syntax"
    if kind in {"command-option", "command-table", "rest-method", "rest-field"}:
        cells = [cell.strip(" *`") for cell in text.strip("|").split("|")]
        return cells[0] if cells and cells[0] else "table-field"
    if kind == "rest-resource":
        path = re.search(r"/rest/[A-Za-z0-9_{}?&=./:-]+", text)
        if path:
            return path.group(0)
        element = re.search(r"<([A-Za-z][A-Za-z0-9_.:-]*)", text)
        if element:
            return f"xml-root:{element.group(1)}"
        key = re.search(r'["\']([A-Za-z][A-Za-z0-9_-]*)["\']\s*:', text)
        return f"payload-root:{key.group(1)}" if key else "structured-payload"
    versions = re.findall(r"\bV?\d+(?:[._RrMm]\d+)+\b", text)
    return (
        "versions:" + ",".join(dict.fromkeys(versions))
        if versions
        else "capability-note"
    )


def extract_source_units(topic_id: str, text: str) -> list[dict[str, object]]:
    """Enumerate structured operation, mode, parameter, and constraint candidates."""
    is_command = topic_id.startswith("commands-")
    section = ""
    in_fence = False
    fence_start = 0
    fence_lines: list[str] = []
    units: list[dict[str, object]] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped[3:].strip().upper()
        if stripped.startswith("```"):
            if in_fence:
                content = " ".join(part.strip() for part in fence_lines if part.strip())
                if content and not is_command:
                    units.append(_unit(topic_id, "rest-resource", fence_start, content))
                fence_lines = []
                in_fence = False
            else:
                in_fence = True
                fence_start = number + 1
            continue
        if in_fence:
            fence_lines.append(line)
            continue
        if is_command and section == "SYNOPSIS" and stripped:
            units.append(_unit(topic_id, "command-synopsis", number, stripped))
            continue
        if stripped.startswith("|") and not TABLE_SEPARATOR.fullmatch(stripped):
            cells = [cell.strip(" *`") for cell in stripped.strip("|").split("|")]
            if is_command:
                kind = "command-option" if section == "OPTIONS" else "command-table"
            else:
                kind = "rest-method" if cells and cells[0] in METHODS else "rest-field"
            units.append(_unit(topic_id, kind, number, stripped))
            continue
        if re.search(
            r"\b(?:Since|Version|HMC version|PowerVM)\b", stripped, re.IGNORECASE
        ):
            units.append(_unit(topic_id, "capability-statement", number, stripped))
    return units


def _index(
    records: Sequence[dict[str, object]], kind: str, errors: list[str]
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for index, record in enumerate(records):
        identity = record.get("id")
        if not isinstance(identity, str) or not ID.fullmatch(identity):
            errors.append(f"{kind}[{index}]: invalid id {identity!r}")
            continue
        if identity in result:
            errors.append(f"{kind}: duplicate id {identity}")
        result[identity] = record
    return result


def _validate_versions(
    documents: Mapping[str, Mapping[str, object]], errors: list[str]
) -> None:
    for name, document in documents.items():
        if name == "maturity.json":
            continue
        if document.get("format_version") != 1:
            errors.append(f"{name}: format_version must be 1")


def _validate_topics(
    corpora: Mapping[str, dict[str, object]],
    topics: Mapping[str, dict[str, object]],
    errors: list[str],
) -> None:
    paths: set[tuple[object, object]] = set()
    for identity, topic in topics.items():
        corpus = topic.get("corpus")
        path = topic.get("path")
        pair = (corpus, path)
        if corpus not in corpora:
            errors.append(f"topic {identity}: unknown corpus {corpus!r}")
        if (
            not isinstance(path, str)
            or Path(path).is_absolute()
            or ".." in Path(path).parts
        ):
            errors.append(f"topic {identity}: unsafe path {path!r}")
        elif pair in paths:
            errors.append(f"topic {identity}: duplicate corpus path {path!r}")
        paths.add(pair)
        if topic.get("classification") not in {
            "operation",
            "schema",
            "overview",
            "navigation",
        }:
            errors.append(f"topic {identity}: invalid classification")
        if not isinstance(topic.get("reason"), str) or not str(topic["reason"]).strip():
            errors.append(f"topic {identity}: classification reason is required")
        if not HEX_256.fullmatch(str(topic.get("sha256", ""))):
            errors.append(f"topic {identity}: invalid sha256")


def _validate_corpora(
    corpora: Mapping[str, dict[str, object]], errors: list[str]
) -> None:
    for identity, corpus in corpora.items():
        if not str(corpus.get("source_url", "")).startswith("https://"):
            errors.append(f"corpus {identity}: HTTPS source_url is required")
        if not HEX_256.fullmatch(str(corpus.get("archive_sha256", ""))):
            errors.append(f"corpus {identity}: invalid archive_sha256")
        for field in ("captured_pages", "navigation_pages"):
            value = corpus.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(
                    f"corpus {identity}: {field} must be a non-negative integer"
                )


def _validate_rows(
    rows: Mapping[str, dict[str, object]],
    units: Mapping[str, dict[str, object]],
    errors: list[str],
) -> tuple[list[str], list[str], list[str]]:
    accounted: Counter[str] = Counter()
    unknown: list[str] = []
    proposed: list[str] = []
    pending: list[str] = []
    for identity, row in rows.items():
        refs = row.get("source_units")
        if not isinstance(refs, list) or not refs:
            errors.append(f"row {identity}: source_units must be a non-empty array")
            continue
        for ref in refs:
            if ref not in units:
                errors.append(f"row {identity}: unknown source unit {ref!r}")
            elif isinstance(ref, str):
                accounted[ref] += 1
        disposition = row.get("disposition")
        if (
            not isinstance(disposition, dict)
            or disposition.get("kind") not in DISPOSITIONS
        ):
            errors.append(f"row {identity}: invalid disposition")
            continue
        kind = str(disposition["kind"])
        if kind == "coverage-child":
            issue = disposition.get("issue")
            if not isinstance(issue, int) or issue < 1:
                errors.append(f"row {identity}: coverage-child requires issue")
            pending.append(identity)
        elif kind == "unknown":
            if not str(disposition.get("question", "")).strip():
                errors.append(f"row {identity}: unknown requires question")
            unknown.append(identity)
        elif kind == "proposed-exclusion":
            if (
                not str(disposition.get("reason", "")).strip()
                or not str(disposition.get("owner", "")).strip()
            ):
                errors.append(
                    f"row {identity}: proposed exclusion requires reason and owner"
                )
            proposed.append(identity)
    for identity, unit in units.items():
        accounting = unit.get("accounting")
        if not isinstance(accounting, dict):
            errors.append(f"source unit {identity}: accounting is required")
            continue
        if accounting.get("kind") == "row":
            row_id = accounting.get("id")
            if row_id not in rows:
                errors.append(
                    f"source unit {identity}: unknown accounting row {row_id!r}"
                )
            elif accounted[identity] != 1 or identity not in cast(
                list[object], rows[str(row_id)].get("source_units", [])
            ):
                errors.append(
                    f"source unit {identity}: accounting does not match row {row_id}"
                )
        elif accounting.get("kind") == "non-operation":
            if not str(accounting.get("reason", "")).strip():
                errors.append(
                    f"source unit {identity}: non-operation reason is required"
                )
        else:
            errors.append(f"source unit {identity}: invalid accounting")
        if accounted[identity] > 1:
            errors.append(f"source unit {identity}: accounted by more than one row")
    return unknown, proposed, pending


def _validate_operations(
    records: Sequence[dict[str, object]],
    rows: Mapping[str, dict[str, object]],
    registry: Collection[RegistryTool],
    repo_root: Path,
    errors: list[str],
) -> None:
    expected = {tool.tool: tool for tool in registry}
    observed: dict[str, dict[str, object]] = {}
    for index, record in enumerate(records):
        tool = record.get("tool")
        if not isinstance(tool, str):
            errors.append(f"operations[{index}]: tool is required")
            continue
        if tool in observed:
            errors.append(f"operation evidence: duplicate tool {tool}")
        observed[tool] = record
        found = expected.get(tool)
        if found is None:
            errors.append(f"operation evidence {tool}: tool is not registered")
            continue
        for field in ("operation", "handler", "signature"):
            if record.get(field) != getattr(found, field):
                errors.append(
                    f"operation evidence {tool}: {field} does not match registry"
                )
        if tuple(cast(Sequence[object], record.get("surfaces", ()))) != found.surfaces:
            errors.append(f"operation evidence {tool}: surfaces do not match registry")
        row_ids = record.get("row_ids")
        if not isinstance(row_ids, list):
            errors.append(f"operation evidence {tool}: row_ids must be an array")
        else:
            for row_id in row_ids:
                if row_id not in rows:
                    errors.append(f"operation evidence {tool}: unknown row {row_id!r}")
        if not row_ids and not str(record.get("composite_reason", "")).strip():
            errors.append(
                f"operation evidence {tool}: rows or composite reason required"
            )
        tests = record.get("tests")
        if not isinstance(tests, list):
            errors.append(f"operation evidence {tool}: tests must be an array")
        else:
            if not tests:
                errors.append(f"operation evidence {tool}: tests must not be empty")
            for path_text in tests:
                path = repo_root / str(path_text)
                if (
                    Path(str(path_text)).is_absolute()
                    or ".." in Path(str(path_text)).parts
                    or not path.is_file()
                    or path.is_symlink()
                ):
                    errors.append(
                        f"operation evidence {tool}: invalid test path {path_text!r}"
                    )
    for tool in sorted(expected.keys() - observed.keys()):
        errors.append(f"registry operation {tool}: missing operation evidence")


def _check_runtime_projection(path: Path, expected: str) -> list[str]:
    """Report a missing or byte-stale generated package projection."""
    try:
        actual = path.read_bytes()
    except OSError as error:
        return [
            (
                f"runtime projection {path} cannot be read: {error}; "
                "run `just capability-metadata`"
            )
        ]
    if actual != expected.encode("utf-8"):
        return [
            (
                f"runtime projection {path} is malformed or stale; "
                "run `just capability-metadata`"
            )
        ]
    return []


def _release_claim_errors(root: Path, operations: Collection[str]) -> list[str]:
    errors: list[str] = []
    try:
        paths = sorted((root / "releases").iterdir())
        files = [path for path in paths if path.suffix == ".json"]
        if not files:
            return [f"{root}: release claims directory must contain a JSON file"]
        for path in files:
            try:
                capability_registry.load_release_claims(path, operations)
            except InventoryError as error:
                errors.append(str(error))
    except OSError as error:
        errors.append(f"{root}: cannot read release claims: {error}")
    return errors


def _maturity_records(inventory: Path) -> list[dict[str, object]]:
    operations = capability_schema.load_json(inventory / "maturity.json").get(
        "operations"
    )
    if not isinstance(operations, list):
        return []
    return [record for record in operations if isinstance(record, dict)]


def _process_runtime_projection(
    records: Sequence[Mapping[str, object]],
    registry: Collection[RegistryTool],
    output: Path | None,
    fingerprints: dict[tuple[Path, str], str],
    inventory: Path,
    *,
    release: bool,
) -> list[str]:
    try:
        snapshot = capability_verification.load_release_verification(
            inventory / "release-verification.json"
        )
        if release:
            derived = json.loads(
                capability_verification.render_release_verification(
                    records,
                    registry,
                    ROOT,
                    datetime.now(UTC),
                    _fingerprints=fingerprints,
                )
            )["operations"]
            if derived != list(snapshot.values()):
                return [
                    "release verification snapshot differs from candidate closures/catalog; record release evidence before releasing"
                ]
        rendered = capability_verification.render_runtime_projection(
            records, registry, snapshot
        )
        if output is None:
            return _check_runtime_projection(DEFAULT_RUNTIME_PROJECTION, rendered)
        capability_verification.write_runtime_projection(output, rendered)
    except (OSError, InventoryError) as error:
        return [f"cannot process runtime projection: {error}"]
    print(f"wrote runtime projection: {output}")
    return []


def verification_report(
    states: Mapping[str, OperationState],
    *,
    fail_on_stale: bool,
    require_current: bool = False,
) -> int:
    """Print every operation's state and the summary; fail only when asked to."""
    in_actions = bool(os.environ.get("GITHUB_ACTIONS"))
    for operation in sorted(states):
        derived = states[operation]
        columns = [operation, derived.implementation, derived.state]
        print("verification: " + " ".join(column for column in columns if column))
        if in_actions and derived.state == "stale":
            print(f"::warning::{operation} is stale: {derived.reason}")
    counts = Counter(derived.state for derived in states.values())
    print(
        "verification coverage: "
        + ", ".join(
            f"{counts.get(name, 0)} {name}"
            for name in ("unrecorded", "unevidenced", "stale", "failed", "current")
        )
        + f" (of {len(states)})"
    )
    _append_step_summary(states)
    if require_current:
        failures = {
            name: state for name, state in states.items() if state.state != "current"
        }
        for name, state in sorted(failures.items()):
            reason = f" ({state.reason})" if state.reason else ""
            print(f"ERROR: {name}: {state.state}{reason}", file=sys.stderr)
        return int(bool(failures))
    return 1 if fail_on_stale and counts.get("stale") else 0


def _append_step_summary(states: Mapping[str, OperationState]) -> None:
    """Append the report as a Markdown table to the GitHub step summary."""
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if not destination:
        return
    lines = [
        "| Operation | Implementation | State | Reason |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {operation} | {states[operation].implementation or ''} "
        f"| {states[operation].state} | {states[operation].reason or ''} |"
        for operation in sorted(states)
    ]
    try:
        with open(destination, "a", encoding="utf-8") as summary:
            summary.write("\n".join(lines) + "\n")
    except OSError as error:
        # The report must never fail a pull request; an unwritable summary path
        # is a runner problem, not evidence that anything is stale.
        print(f"could not append the step summary: {error}", file=sys.stderr)


def validate_inventory(
    root: Path,
    registry: Collection[RegistryTool],
    *,
    repo_root: Path = ROOT,
) -> Report:
    errors: list[str] = []
    try:
        documents = {
            name: capability_schema.load_json(root / name)
            for name in (
                "corpora.json",
                "rows.json",
                "operations.json",
                "maturity.json",
            )
        }
    except InventoryError as error:
        return Report((str(error),), (), (), (), 0, 0, 0, 0, 0)
    _validate_versions(documents, errors)
    maturity_document = documents["maturity.json"]
    capability_schema.exact_keys(
        maturity_document,
        {"format_version", "admission_policy", "operations"},
        "maturity.json",
        errors,
    )
    if (
        maturity_document.get("format_version")
        != capability_schema.MATURITY_FORMAT_VERSION
        or type(maturity_document.get("format_version")) is not int
    ):
        errors.append(
            f"maturity.json: format_version must be integer {capability_schema.MATURITY_FORMAT_VERSION}"
        )
    if maturity_document.get("admission_policy") != "existing-runtime-guards":
        errors.append("maturity.json: admission_policy must be existing-runtime-guards")
    corpora_records = capability_schema.objects(
        capability_schema.array(documents["corpora.json"], "corpora", errors),
        "corpora",
        errors,
    )
    topic_records = capability_schema.objects(
        capability_schema.array(documents["corpora.json"], "topics", errors),
        "topics",
        errors,
    )
    unit_records = capability_schema.objects(
        capability_schema.array(documents["corpora.json"], "source_units", errors),
        "source_units",
        errors,
    )
    row_records = capability_schema.objects(
        capability_schema.array(documents["rows.json"], "rows", errors), "rows", errors
    )
    operation_records = capability_schema.objects(
        capability_schema.array(documents["operations.json"], "operations", errors),
        "operations",
        errors,
    )
    maturity_records = capability_schema.objects(
        capability_schema.array(documents["maturity.json"], "operations", errors),
        "maturity operations",
        errors,
    )
    corpora = _index(corpora_records, "corpora", errors)
    topics = _index(topic_records, "topics", errors)
    units = _index(unit_records, "source_units", errors)
    rows = _index(row_records, "rows", errors)
    _validate_corpora(corpora, errors)
    _validate_topics(corpora, topics, errors)
    for identity, unit in units.items():
        if unit.get("topic") not in topics:
            errors.append(
                f"source unit {identity}: unknown topic {unit.get('topic')!r}"
            )
        if not HEX_256.fullmatch(str(unit.get("sha256", ""))):
            errors.append(f"source unit {identity}: invalid sha256")
    unknown, proposed, pending = _validate_rows(rows, units, errors)
    _validate_operations(operation_records, rows, registry, repo_root, errors)
    operation_ids = {
        operation
        for record in operation_records
        if isinstance(operation := record.get("operation"), str)
    }
    capability_schema.validate_maturity(maturity_records, operation_ids, errors)
    maturity_ids = {
        operation
        for record in maturity_records
        if isinstance(operation := record.get("operation"), str)
    }
    for operation in sorted(operation_ids - maturity_ids):
        errors.append(f"maturity operation {operation}: missing maturity record")
    errors.extend(_release_claim_errors(root, operation_ids))
    return Report(
        tuple(errors),
        tuple(sorted(unknown)),
        tuple(sorted(proposed)),
        tuple(sorted(pending)),
        len(topics),
        len(units),
        len(rows),
        len(operation_records),
        len(maturity_records),
    )


def verify_corpora(root: Path, sources: Mapping[str, Path]) -> list[str]:
    try:
        document = capability_schema.load_json(root / "corpora.json")
    except InventoryError as error:
        return [str(error)]
    local_errors: list[str] = []
    corpus_records = capability_schema.objects(
        values if isinstance(values := document.get("corpora"), list) else [],
        "corpora",
        local_errors,
    )
    corpus_ids = {str(record.get("id")) for record in corpus_records}
    topics = capability_schema.objects(
        values if isinstance(values := document.get("topics"), list) else [],
        "topics",
        local_errors,
    )
    units = capability_schema.objects(
        values if isinstance(values := document.get("source_units"), list) else [],
        "source_units",
        local_errors,
    )
    expected_paths: dict[str, set[str]] = {name: set() for name in sources}
    expected_units: dict[str, list[dict[str, object]]] = {}
    errors = local_errors
    for corpus in sorted(corpus_ids - sources.keys()):
        errors.append(f"{corpus}: source root was not supplied")
    for corpus in sorted(sources.keys() - corpus_ids):
        errors.append(f"{corpus}: unexpected source corpus")
    for topic in topics:
        corpus = str(topic.get("corpus"))
        if corpus not in sources:
            continue
        relative = str(topic.get("path"))
        expected_paths[corpus].add(relative)
        path = sources[corpus] / relative
        if not path.is_file() or path.is_symlink():
            errors.append(f"{corpus}/{relative}: missing or non-regular source file")
            continue
        digest = format_sha256(path.read_bytes())
        if digest != topic.get("sha256"):
            errors.append(f"{corpus}/{relative}: hash mismatch")
        try:
            expected_units[str(topic["id"])] = extract_source_units(
                str(topic["id"]), path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError) as error:
            errors.append(f"{corpus}/{relative}: cannot read: {error}")
    for corpus, source in sources.items():
        if not source.is_dir() or source.is_symlink():
            errors.append(
                f"{corpus}: source root is missing, symlinked, or not a directory"
            )
            continue
        actual = {
            str(path.relative_to(source))
            for path in source.rglob("*.md")
            if path.is_file() and not path.is_symlink()
        }
        for relative in sorted(actual - expected_paths[corpus]):
            errors.append(f"{corpus}/{relative}: unexpected source file")
    recorded_by_topic: dict[str, list[dict[str, object]]] = {}
    for unit in units:
        clean = {key: value for key, value in unit.items() if key != "accounting"}
        recorded_by_topic.setdefault(str(unit.get("topic")), []).append(clean)
    for topic_id, expected in expected_units.items():
        if recorded_by_topic.get(topic_id, []) != expected:
            errors.append(f"{topic_id}: derived source units differ")
    return errors


def _source(value: str) -> tuple[str, Path]:
    name, separator, path = value.partition("=")
    if not separator or not name or not path:
        raise argparse.ArgumentTypeError("source must be CORPUS=PATH")
    return name, Path(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--source", action="append", default=[], type=_source)
    parser.add_argument(
        "--verification-report",
        action="store_true",
        help="print each operation's derived live-verification state",
    )
    parser.add_argument(
        "--fail-on-stale",
        action="store_true",
        help="exit non-zero when any operation's evidence has gone stale",
    )
    parser.add_argument(
        "--require-current",
        type=Path,
        help="report and require current candidate evidence for a release claims JSON file",
    )
    parser.add_argument(
        "--write-runtime-projection",
        type=Path,
        help="atomically write the generated runtime projection to PATH",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="require the release snapshot to match candidate closures",
    )
    args = parser.parse_args(argv)
    if args.require_current is not None and args.fail_on_stale:
        parser.error("cannot combine --require-current with --fail-on-stale")
    if args.fail_on_stale and not args.verification_report:
        parser.error("--fail-on-stale requires --verification-report")
    try:
        registry = capability_registry.discover_registry()
    except InventoryError as error:
        print(f"capability inventory: {error}", file=sys.stderr)
        return 1
    report = validate_inventory(args.inventory, registry)
    errors = list(report.errors)
    selected_registry = registry
    if args.require_current is not None:
        try:
            claims = capability_registry.load_release_claims(
                args.require_current, {tool.operation for tool in registry}
            )
            selected = {claim["operation"] for claim in claims}
            selected_registry = tuple(
                tool for tool in registry if tool.operation in selected
            )
        except InventoryError as error:
            errors.append(str(error))
    if args.source:
        sources = dict(args.source)
        if len(sources) != len(args.source):
            errors.append("duplicate --source corpus ID")
        errors.extend(verify_corpora(args.inventory, sources))
    maturity_records: list[dict[str, object]] = []
    fingerprints: dict[tuple[Path, str], str] = {}
    if not errors:
        maturity_records = _maturity_records(args.inventory)
        errors.extend(
            _process_runtime_projection(
                maturity_records,
                registry,
                args.write_runtime_projection,
                fingerprints,
                args.inventory,
                release=args.release,
            )
        )
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        return 1
    if args.verification_report or args.require_current is not None:
        # Derived staleness is never a validation error, so the report runs only
        # after validation has passed and reports separately from it.
        states = capability_verification.derive_states(
            maturity_records,
            selected_registry,
            ROOT,
            datetime.now(UTC),
            _fingerprints=fingerprints,
        )
        return verification_report(
            states,
            fail_on_stale=args.fail_on_stale,
            require_current=args.require_current is not None,
        )
    print(
        "capability inventory: structurally valid; "
        f"{report.topic_count} topics, {report.source_unit_count} source units, "
        f"{report.row_count} rows, {report.operation_count} registry operations, "
        f"{report.maturity_operation_count} maturity operations"
    )
    if report.complete:
        print("capability coverage: complete")
    else:
        print(
            "capability coverage: incomplete; "
            f"{len(report.pending_ids)} coverage children, "
            f"{len(report.unknown_ids)} unknowns, "
            f"{len(report.proposed_exclusion_ids)} proposed exclusions"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
