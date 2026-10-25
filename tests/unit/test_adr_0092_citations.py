"""Keep ADR 0092's maintained source citations tied to parsed Python symbols.

The historical §Context survey is intentionally excluded. Maintained §§3–5 use
``path.py::symbol`` citations: definitions identify the named operation, while a
guard citation identifies the enclosing function whose call graph must reach an
ownership helper. Neither form depends on a line offset.
"""

import ast
import re
from functools import cache
from pathlib import Path
from typing import NamedTuple

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ADR_PATH = (
    _REPO_ROOT / "docs" / "adr" / "0092-uniform-lpar-ownership-authorization-rule.md"
)
_SOURCE_ROOT = _REPO_ROOT / "src" / "hmcpctl"
_SECTION_HEADING = "### 3. Classification"
_NEXT_SECTION_HEADING = "### 6."
_SYMBOL_CITATION = re.compile(
    r"`(?P<path>[\w./-]+\.py)::(?P<symbol>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)`"
)
_LINE_CITATION = re.compile(r"`[\w./-]+\.py:\d+`")
_CITATION_LIKE = re.compile(r"[\w./*-]+\.py::?")
_BACKTICKED = re.compile(r"`([^`]+)`")
_MINIMUM_ROWS = 40

_MUTATOR_PREFIXES = (
    "change_lpar_processors",
    "add_",
    "apply_",
    "assign_",
    "attach_",
    "abort_",
    "clear_",
    "configure_",
    "create_",
    "decommission_",
    "delete_",
    "deploy_",
    "detach_",
    "install_",
    "map_",
    "migrate_",
    "modify_",
    "mount_",
    "power_",
    "provision_",
    "recover_",
    "remote_",
    "remove_",
    "rename_",
    "restore_",
    "set_",
    "synchronize_",
    "submit_power_on_lpar",
    "unassign_",
    "unmount_",
)
_INVENTORY_PATHS = tuple(
    _SOURCE_ROOT / path
    for path in (
        "operations/lpar/activation.py",
        "operations/lpar/assignments.py",
        "operations/lpar/boot_order.py",
        "operations/lpar/configuration.py",
        "operations/lpar/vtpm.py",
        "operations/lpar/core.py",
        "operations/lpar/decommission.py",
        "operations/lpar/dlpar.py",
        "operations/lpar/processors.py",
        "operations/lpar/migration.py",
        "operations/lpar/ownership.py",
        "operations/lpar/provision.py",
        "operations/affinity/ssh.py",
        "operations/virtualization/adapters.py",
        "operations/virtualization/pcie.py",
        "operations/virtualization/vnic.py",
        "operations/storage/resources.py",
        "operations/templates/core.py",
        "operations/vios/install.py",
    )
)
_CLASSIFIED_MUTATORS = frozenset(
    {
        "apply_lpar_pcie_assignments",
        "apply_validated_lpar_pcie_assignments",
        "assign_dedicated_pcie_slot",
        "unassign_dedicated_pcie_slot",
        "assign_sriov_logical_port",
        "unassign_sriov_logical_port",
        "attach_disk_to_lpar",
        "abort_lpar_migration",
        "recover_lpar_migration",
        "remote_restart_lpar",
        "add_network_adapter",
        "add_vscsi_adapter",
        "add_vfc_adapter",
        "delete_adapter",
        "add_vnic",
        "remove_vnic",
        "clear_lpar_boot_order",
        "set_lpar_boot_order",
        "configure_lpar_settings",
        "configure_lpar_reporting_settings",
        "configure_lpar_vtpm",
        "configure_lpar_msp",
        "configure_lpar_processor_compatibility",
        "synchronize_lpar_profile",
        "restore_system_lpar_profiles",
        "delete_lpar",
        "rename_lpar",
        "decommission_lpar",
        "modify_lpar",
        "set_lpar_processors",
        "change_lpar_processors",
        "set_lpar_memory",
        "migrate_lpar",
        "migrate_lpar_with_affinity_preflight",
        "map_storage",
        "detach_storage_mapping",
        "mount_optical_media",
        "unmount_optical_media",
        "set_minimum_affinity_policy",
        "set_lpar_ownership_description",
    }
)
_OPERATIONAL_MUTATORS = frozenset({"submit_power_on_lpar", "power_off_lpar"})
_CREATION_MUTATORS = frozenset(
    {"create_and_stamp_lpar", "provision_lpar", "deploy_partition_template"}
)
_NON_LPAR_MUTATORS = frozenset(
    {
        "create_volume_group",
        "create_virtual_disk",
        "delete_virtual_disk",
        "create_media_repository",
        "create_optical_media",
        "delete_media_repository",
        "delete_optical_media",
        "power_on_lpar",
        "set_sriov_adapter_mode",
        "install_vios",
    }
)
_GUARDED_DELEGATES = {
    "add_vnic": "_preflight_add",
    "assign_sriov_logical_port": "_preflight_sriov_assignment",
    "apply_lpar_pcie_assignments": "apply_validated_lpar_pcie_assignments",
    "migrate_lpar_with_affinity_preflight": "migrate_lpar",
}
_UNCHECKED_ROWS = frozenset({"`hmc lpar modify` (CLI)"})


class Citation(NamedTuple):
    symbol: str
    path: Path
    raw_path: str

    def __str__(self) -> str:
        return f"{self.raw_path}::{self.symbol}"


def _source_path(raw_path: str) -> Path:
    return (
        _REPO_ROOT / raw_path
        if raw_path.startswith("tests/")
        else _SOURCE_ROOT / raw_path
    )


def _maintained_lines() -> list[str]:
    lines = _ADR_PATH.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(_SECTION_HEADING)]
    assert len(starts) == 1, f"{_ADR_PATH.name} has {len(starts)} '§3' headings"
    ends = [
        i
        for i, line in enumerate(lines)
        if i > starts[0] and line.startswith(_NEXT_SECTION_HEADING)
    ]
    assert ends, f"{_ADR_PATH.name} has no §6 heading to bound §§3–5"
    return lines[starts[0] : ends[0]]


def _parse_citations() -> list[Citation]:
    """Parse every maintained symbol citation and reject stale line references."""
    lines = _maintained_lines()
    assert not any(_LINE_CITATION.search(line) for line in lines), (
        "maintained ADR 0092 citations must use path.py::symbol, not line numbers"
    )
    for line in lines:
        for token in _BACKTICKED.findall(line):
            if _CITATION_LIKE.search(token):
                assert _SYMBOL_CITATION.fullmatch(f"`{token}`"), (
                    f"malformed maintained citation `{token}`; expected path.py::symbol"
                )
    citations = [
        Citation(
            match.group("symbol"),
            _source_path(match.group("path")),
            match.group("path"),
        )
        for line in lines
        for match in _SYMBOL_CITATION.finditer(line)
    ]
    assert citations, "ADR 0092 §§3–5 contain no symbol citations"
    return citations


def _row_symbol_citations() -> list[tuple[str, list[Citation], list[Citation]]]:
    """Return §3 rows with definition citations and guard/enclosing citations."""
    rows = []
    for line in _maintained_lines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        symbols = [
            token for token in _BACKTICKED.findall(cells[0]) if token.isidentifier()
        ]
        if not symbols:
            assert not _SYMBOL_CITATION.search(line) or cells[0] in _UNCHECKED_ROWS, (
                f"§3 row cites a symbol but names no Python definition: {line}"
            )
            if cells[0] in _UNCHECKED_ROWS:
                definitions = [
                    Citation(
                        match.group("symbol"),
                        _source_path(match.group("path")),
                        match.group("path"),
                    )
                    for match in _SYMBOL_CITATION.finditer(cells[1])
                ]
                guards = [
                    Citation(
                        match.group("symbol"),
                        _source_path(match.group("path")),
                        match.group("path"),
                    )
                    for cell in cells[2:]
                    for match in _SYMBOL_CITATION.finditer(cell)
                ]
                rows.append((cells[0], definitions, guards))
            continue
        definition_matches = [
            match for cell in cells[:2] for match in _SYMBOL_CITATION.finditer(cell)
        ]
        definitions = [
            Citation(symbol, _source_path(match.group("path")), match.group("path"))
            for symbol, match in zip(symbols, definition_matches)
        ]
        guards = [
            Citation(
                match.group("symbol"),
                _source_path(match.group("path")),
                match.group("path"),
            )
            for cell in cells[2:]
            for match in _SYMBOL_CITATION.finditer(cell)
        ]
        assert len(symbols) == len(definitions), (
            f"§3 row names {len(symbols)} symbols but cites {len(definitions)} definitions: {line}"
        )
        rows.append((cells[0], definitions, guards))
    return rows


@cache
def _definitions(path: Path) -> dict[str, ast.AST]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, ast.AST] = {}

    def visit(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                found[name] = child
                visit(child, name)
            else:
                visit(child, prefix)

    visit(tree)
    return found


try:
    _CITATIONS = _parse_citations()
    _ROWS = _row_symbol_citations()
    _PARSE_FAILURE: str | None = None
except AssertionError as exc:
    _CITATIONS = []
    _ROWS = []
    _PARSE_FAILURE = str(exc)


def test_section_3_rows_are_all_parsed() -> None:
    assert _PARSE_FAILURE is None, _PARSE_FAILURE
    assert len(_ROWS) >= _MINIMUM_ROWS, (
        f"parsed only {len(_ROWS)} §3 rows; the table format changed and this guard is no longer reading it"
    )


def test_maintained_citations_are_well_formed() -> None:
    assert _PARSE_FAILURE is None, _PARSE_FAILURE
    assert all(citation.path.is_file() for citation in _CITATIONS)


def test_every_exposed_mutator_has_an_ownership_classification() -> None:
    discovered = {
        node.name
        for path in _INVENTORY_PATHS
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name.startswith(_MUTATOR_PREFIXES)
    }
    classified = (
        _CLASSIFIED_MUTATORS
        | _OPERATIONAL_MUTATORS
        | _CREATION_MUTATORS
        | _NON_LPAR_MUTATORS
    )
    assert discovered == classified, (
        "ADR 0092 ownership inventory is incomplete: "
        f"unclassified={sorted(discovered - classified)}, stale={sorted(classified - discovered)}"
    )


def test_guarded_mutators_reach_an_ownership_helper() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in _INVENTORY_PATHS)
    for operation in _CLASSIFIED_MUTATORS:
        definition = re.search(
            rf"async def {operation}\b.*?(?=\nasync def |\Z)", source, re.DOTALL
        )
        assert definition is not None
        body = definition.group()
        delegate = _GUARDED_DELEGATES.get(operation)
        assert "authorize" in body or (delegate is not None and delegate in body), (
            operation
        )


def _reaches_guard(
    path: Path, symbol: str, seen: set[tuple[Path, str]] | None = None
) -> bool:
    seen = set() if seen is None else seen
    key = (path, symbol)
    if key in seen:
        return False
    seen.add(key)
    node = _definitions(path).get(symbol)
    if node is None:
        return False
    calls = {
        child.func.id
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
    }
    if any("authorize" in name for name in calls):
        return True
    return any(
        _reaches_guard(path, name, seen) for name in calls if name in _definitions(path)
    )


@pytest.mark.parametrize("citation", _CITATIONS, ids=str)
def test_maintained_symbol_citation_names_a_definition(citation: Citation) -> None:
    assert citation.path.is_file(), (
        f"ADR 0092 cites {citation.raw_path}, which does not exist"
    )
    assert citation.symbol in _definitions(citation.path), (
        f"ADR 0092 cites `{citation.raw_path}::{citation.symbol}`, but that symbol is not defined"
    )


def test_guard_citations_name_enclosing_functions_that_reach_a_guard() -> None:
    assert _PARSE_FAILURE is None, _PARSE_FAILURE
    for row, _, guards in _ROWS:
        if "guarded" not in row:
            continue
        assert guards, f"guarded §3 row has no enclosing-function citation: {row}"
        for citation in guards:
            assert _reaches_guard(citation.path, citation.symbol), (
                f"guard citation `{citation.raw_path}::{citation.symbol}` does not reach an ownership helper"
            )


def test_context_citations_are_explicitly_exempt() -> None:
    context = _ADR_PATH.read_text(encoding="utf-8").split("## Decision", 1)[0]
    assert _LINE_CITATION.search(context)


def test_malformed_symbol_citations_are_rejected() -> None:
    malformed = "`operations/lpar/core.py:403`"
    assert _LINE_CITATION.fullmatch(malformed)
    assert not _SYMBOL_CITATION.fullmatch(malformed)


def test_processor_quantity_row_and_guard() -> None:
    section = _ADR_PATH.read_text().split("#### 3.2 ", 1)[1].split("#### 3.3 ", 1)[0]
    rows = [
        line
        for line in section.splitlines()
        if line.startswith("| `change_lpar_processors` |")
    ]
    assert len(rows) == 1
    citations = [
        citation
        for row, locations, guards in _ROWS
        if row == "`change_lpar_processors`"
        for citation in (*locations, *guards)
    ]
    assert citations and all(c.symbol == "change_lpar_processors" for c in citations)
    assert _reaches_guard(
        _SOURCE_ROOT / "operations/lpar/processors.py", "change_lpar_processors"
    )
