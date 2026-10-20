"""Tests for declared live scenario definitions (scripts/live_test/definitions.py)."""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from live_test import definitions, runner  # noqa: E402
from live_test.observation import SCENARIO_ID  # noqa: E402

from hmcpctl.server import TOOL_SECURITY  # noqa: E402
from hmcpctl.tool_registry import TargetSelector, ToolSecurity  # noqa: E402

SECURITY = {
    "hmc_get_lpar": ToolSecurity(
        "read",
        "lpar.get",
        "lpar",
        (
            TargetSelector("lpar", "lpar_name_or_uuid", True),
            TargetSelector("managed_system", "system_name_or_uuid", False),
        ),
    ),
    "hmc_power_on_lpar": ToolSecurity(
        "mutate", "lpar.power_on", "lpar", (TargetSelector("lpar", "lpar_uuid", True),)
    ),
    "hmc_plan_lpar": ToolSecurity(
        "read",
        "lpar.plan",
        "vios",
        (TargetSelector("vios", "vios_uuid", True, container="storage"),),
        connection_argument=None,
    ),
}
READ = {
    "scenario": "st100-lpar-get",
    "operation": "lpar.get",
    "class": "read-only",
    "arguments": {"lpar_name_or_uuid": {"target": "lpar"}, "detail": True},
    "expected": "success",
}
MUTATE = {
    "scenario": "st100-lpar-power-on",
    "operation": "lpar.power_on",
    "class": "mutating",
    "arguments": {"lpar_uuid": {"target": "lpar"}},
    "expected": "success",
}


def _document(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"format_version": 1, "definitions": list(entries)}


def _with(**changes: Any) -> dict[str, Any]:
    entry = copy.deepcopy(READ)
    entry.update(changes)
    return entry


def test_valid_read_only_and_mutating_definitions_load():
    loaded = definitions.load_definitions(_document(READ, MUTATE), SECURITY)

    assert [item.scenario for item in loaded] == [
        "st100-lpar-get",
        "st100-lpar-power-on",
    ]
    read, mutate = loaded
    assert (read.tool, read.read_only, read.expected) == (
        "hmc_get_lpar",
        True,
        "success",
    )
    assert dict(read.arguments) == {
        "lpar_name_or_uuid": definitions.Placeholder("lpar"),
        "detail": True,
    }
    assert (mutate.tool, mutate.read_only) == ("hmc_power_on_lpar", False)


def test_the_committed_registry_loads_against_the_served_registry():
    loaded = definitions.read_definitions()

    assert loaded
    assert all(
        item.scenario == definitions.scenario_id(item.operation) for item in loaded
    )


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ([], "object with exactly format_version and definitions"),
        ({"definitions": []}, "object with exactly format_version and definitions"),
        (
            {**_document(), "extra": 1},
            "object with exactly format_version and definitions",
        ),
        ({"format_version": 2, "definitions": []}, "unsupported format_version"),
        ({"format_version": True, "definitions": []}, "unsupported format_version"),
        ({"format_version": 1, "definitions": {}}, "definitions must be a list"),
    ],
)
def test_malformed_documents_are_rejected(document, message):
    with pytest.raises(definitions.DefinitionError, match=message):
        definitions.load_definitions(document, SECURITY)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("st100-lpar-get", "must be an object with exactly the fields"),
        ({k: v for k, v in READ.items() if k != "expected"}, "exactly the fields"),
        ({**READ, "runnable": True}, "exactly the fields"),
        (_with(operation="Lpar.get"), "operation is not an operation ID"),
        (_with(operation=["lpar.get"]), "operation is not an operation ID"),
        (_with(operation="lpar.delete"), "lpar.delete is not a registered"),
        (_with(scenario="st100-lpar-get-detail"), "scenario must be st100-lpar-get"),
        (_with(scenario="st1-lpar-get"), "scenario must be st100-lpar-get"),
        (_with(scenario=100), "scenario must be st100-lpar-get"),
        (_with(**{"class": "mutating"}), "class mutating disagrees with hmc_get_lpar"),
        (_with(**{"class": "write"}), "class must be read-only or mutating"),
        (_with(**{"class": ["read-only"]}), "class must be read-only or mutating"),
        (_with(expected="failure"), "expected must be success"),
        (_with(expected=["success"]), "expected must be success"),
        (_with(arguments=[]), "arguments must be an object"),
        (
            _with(arguments={"lpar_name_or_uuid": "lab-lpar-1"}),
            'lpar_name_or_uuid must be the placeholder {"target": "lpar"}',
        ),
        (
            _with(arguments={"lpar_name_or_uuid": {"target": "vios"}}),
            'lpar_name_or_uuid must be the placeholder {"target": "lpar"}',
        ),
        (
            _with(arguments={"detail": {"target": "lpar"}}),
            "detail is not a target selector and takes no placeholder",
        ),
        (
            _with(arguments={"lpar_name_or_uuid": {"target": "lpar"}, "profile": "p"}),
            "argument profile may not be supplied",
        ),
    ],
)
def test_each_rule_rejects_a_violating_definition(entry, message):
    with pytest.raises(definitions.DefinitionError, match=re.escape(message)) as error:
        definitions.load_definitions(_document(MUTATE, entry), SECURITY)

    assert str(error.value).startswith("definition 1: ")


def test_a_nested_selector_container_may_not_be_supplied():
    entry = {
        "scenario": "st100-lpar-plan",
        "operation": "lpar.plan",
        "class": "read-only",
        "arguments": {"storage": {"vios_uuid": "x"}},
        "expected": "success",
    }

    with pytest.raises(
        definitions.DefinitionError, match="storage may not be supplied"
    ):
        definitions.load_definitions(_document(entry), SECURITY)


def test_a_duplicate_scenario_is_rejected():
    with pytest.raises(
        definitions.DefinitionError, match="duplicate scenario st100-lpar-get"
    ):
        definitions.load_definitions(_document(READ, READ), SECURITY)


def test_read_definitions_reports_unreadable_and_invalid_files(tmp_path):
    missing = tmp_path / "absent.json"
    with pytest.raises(definitions.DefinitionError, match="cannot read the registry"):
        definitions.read_definitions(missing, SECURITY)

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(definitions.DefinitionError, match="invalid JSON"):
        definitions.read_definitions(invalid, SECURITY)

    duplicated = tmp_path / "duplicated.json"
    duplicated.write_text(
        '{"format_version": 1, "format_version": 1, "definitions": []}',
        encoding="utf-8",
    )
    with pytest.raises(
        definitions.DefinitionError, match="duplicate key format_version"
    ):
        definitions.read_definitions(duplicated, SECURITY)

    undecodable = tmp_path / "undecodable.json"
    undecodable.write_bytes(b"\xff\xfe{")
    with pytest.raises(definitions.DefinitionError, match="not UTF-8"):
        definitions.read_definitions(undecodable, SECURITY)

    deep = tmp_path / "deep.json"
    deep.write_text("[" * 100_000, encoding="utf-8")
    with pytest.raises(definitions.DefinitionError, match="invalid JSON"):
        definitions.read_definitions(deep, SECURITY)

    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps(_document(READ)), encoding="utf-8")
    assert [d.scenario for d in definitions.read_definitions(valid, SECURITY)] == [
        "st100-lpar-get"
    ]


def test_scenario_ids_are_injective_over_registered_operations():
    operations = {security.operation for security in TOOL_SECURITY.values()}
    ids = {definitions.scenario_id(operation) for operation in operations}

    assert len(ids) == len(operations)
    assert all(SCENARIO_ID.fullmatch(item) for item in ids)


def test_no_hand_written_scenario_uses_the_reserved_number():
    reserved = f"st{definitions.SCENARIO_NUMBER}-"
    sources = [
        path
        for path in (ROOT / "scripts" / "live_test").glob("*.py")
        if reserved in path.read_text(encoding="utf-8")
        and path.name != "definitions.py"
    ]

    assert sources == []
    assert definitions.SCENARIO_NUMBER not in runner.SUBTASKS
