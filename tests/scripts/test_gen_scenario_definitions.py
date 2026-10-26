"""Tests for the scenario definition generator (scripts/gen_scenario_definitions.py)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
SCRIPTS = ROOT / "scripts"
# The generator imports `live_test` as a top-level package, exactly as running a
# script from `scripts/` would.
sys.path.insert(0, str(SCRIPTS))
MODULE_SPEC = importlib.util.spec_from_file_location(
    "gen_scenario_definitions", SCRIPTS / "gen_scenario_definitions.py"
)
assert MODULE_SPEC is not None
assert MODULE_SPEC.loader is not None
generator = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = generator
MODULE_SPEC.loader.exec_module(generator)

from live_test import definitions  # noqa: E402

from hmcpctl.server import TOOL_SECURITY  # noqa: E402
from hmcpctl.tool_registry import TargetSelector, ToolSecurity  # noqa: E402

LPAR = TargetSelector("lpar", "lpar_name_or_uuid", True)
SYSTEM = TargetSelector("managed_system", "system_name_or_uuid", False)
STORAGE = TargetSelector("vios", "vios_uuid", True, container="storage")


def _schema(*required: str, optional: tuple[str, ...] = ()) -> dict[str, Any]:
    names = (*required, *optional)
    return {
        "properties": {name: {"type": "string"} for name in names},
        "required": list(required),
    }


def _security(effect: str, *targets: TargetSelector) -> ToolSecurity:
    return ToolSecurity(effect, "lpar.get", "lpar", targets)


@pytest.fixture(scope="module")
def generated() -> tuple[dict[str, Any], list[str]]:
    return generator.generate(
        generator.ledger_operations(), TOOL_SECURITY, generator.served_schemas()
    )


def test_a_read_supplies_only_its_required_targets_as_placeholders():
    derived = generator.derive(
        "lpar.get",
        "hmc_get_lpar",
        _security("read", LPAR, SYSTEM),
        _schema("lpar_name_or_uuid", optional=("system_name_or_uuid", "detail")),
    )

    assert derived == {
        "scenario": "st100-lpar-get",
        "operation": "lpar.get",
        "class": "read-only",
        "arguments": {"lpar_name_or_uuid": {"target": "lpar"}},
        "expected": "success",
    }


@pytest.mark.parametrize(
    ("security", "schema", "reason"),
    [
        (None, _schema(), "tool hmc_get_lpar is not registered"),
        (_security("read", LPAR), None, "tool hmc_get_lpar is not registered"),
        (
            _security("read", LPAR),
            _schema("lpar_name_or_uuid", "port_vlan_id"),
            "required argument port_vlan_id is not a target selector",
        ),
        (
            _security("read", STORAGE),
            _schema("adapters", "storage"),
            "required argument storage is a nested selector's container",
        ),
    ],
)
def test_an_underivable_operation_gets_a_reason(security, schema, reason):
    assert generator.derive("lpar.get", "hmc_get_lpar", security, schema) == reason


def test_a_mutation_needing_only_targets_and_ownership_is_mutating():
    derived = generator.derive(
        "lpar.delete",
        "hmc_delete_lpar",
        _security("destructive", LPAR, SYSTEM),
        _schema(
            "lpar_name_or_uuid",
            optional=("system_name_or_uuid", "ownership_override", "profile"),
        ),
    )

    assert isinstance(derived, dict)
    assert (derived["scenario"], derived["class"]) == ("st100-lpar-delete", "mutating")


@pytest.mark.parametrize("argument", ["wait", "resources", "container"])
def test_a_mutation_with_any_other_argument_is_hand_written(argument):
    security = _security(
        "mutate",
        LPAR,
        TargetSelector("vios", "vios_uuid", False, container="container"),
    )

    assert generator.derive(
        "lpar.modify",
        "hmc_modify_lpar",
        security,
        _schema("lpar_name_or_uuid", optional=(argument,)),
    ) == (f"mutating with optional {argument} a definition would leave at its default")


def test_generate_sorts_by_operation_and_reports_the_rest():
    security = {
        "hmc_list_lpars": ToolSecurity("read", "lpar.list", "managed_system", ()),
        "hmc_get_lpar": ToolSecurity("read", "lpar.get", "lpar", (LPAR,)),
    }
    operations = [
        {"operation": "lpar.list", "tool": "hmc_list_lpars"},
        {"operation": "lpar.get", "tool": "hmc_get_lpar"},
        {"operation": "lpar.gone", "tool": "hmc_gone"},
    ]
    schemas = {
        "hmc_list_lpars": _schema(),
        "hmc_get_lpar": _schema("lpar_name_or_uuid"),
    }

    document, hand_written = generator.generate(operations, security, schemas)

    assert [entry["operation"] for entry in document["definitions"]] == [
        "lpar.get",
        "lpar.list",
    ]
    assert hand_written == ["hand-written: lpar.gone: tool hmc_gone is not registered"]


def test_committed_registry_is_fresh(generated):
    document, _ = generated

    assert generator.render(document) == definitions.DEFINITIONS_PATH.read_text(
        encoding="utf-8"
    ), "the committed registry is stale: run `just scenario-definitions` and commit it"


def test_nested_container_operations_are_hand_written(generated):
    _, hand_written = generated

    for operation in ("lpar.plan", "provision.lpar"):
        assert (
            f"hand-written: {operation}: required argument storage is a nested "
            "selector's container"
        ) in hand_written


def test_main_writes_output(tmp_path, capsys):
    output = tmp_path / "scenario_definitions.json"

    assert generator.main(["--output", str(output)]) == 0

    loaded = definitions.read_definitions(output)
    lines = capsys.readouterr().out.splitlines()
    mutating = sum(not definition.read_only for definition in loaded)
    assert lines[-1] == (
        f"summary: {len(loaded)} definitions ({len(loaded) - mutating} read-only, "
        f"{mutating} mutating); {len(lines) - 1} operations need a hand-written "
        "scenario"
    )
    assert json.loads(output.read_text(encoding="utf-8"))["format_version"] == 1


def test_typed_processor_move_keeps_its_named_hand_written_scenario(generated):
    from live_test import processor_move, runner

    document, hand_written = generated
    assert not any(
        entry["operation"] == "lpar.move_processors"
        for entry in document["definitions"]
    )
    assert (
        "hand-written: lpar.move_processors: required argument move is not a target selector"
        in hand_written
    )
    assert runner.SUBTASKS[61] is processor_move.exercise_processor_move
    assert runner.SUBTASK_GROUPS[processor_move.GROUP] == [61]
    assert processor_move.SCENARIO == "st61-lpar-processor-move"
